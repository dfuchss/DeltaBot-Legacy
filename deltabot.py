from os import environ
from threading import Lock
from typing import Dict, List, Optional

from discord import Status, User, Activity, ActivityType, TextChannel, Message
from discord_components import DiscordComponents, Component, InteractionType, Interaction

from bot_base import BotBase, send_help_message, send, is_direct, delete
from cognitive import IntentResult, EntityResult
from dialog_management import Dialog, DialogResult
from dialogs.admin_dialogs import Cleanup
from dialogs.choose_dialog import Choose
from dialogs.generic_dialogs import NotUnderstanding
from dialogs.misc_dialogs import Clock
from dialogs.news_dialog import News
from dialogs.qna import QnA, QnAAnswer
from system_commands import handle_system
from user_commands.commands import handle_user, init_user_commands, handle_user_button, handle_user_selection
from utils import get_guild, get_components, MethodVersionStore


class BotInstance:
    def __init__(self, bot):
        self._bot = bot
        """The bot itself"""

        self._dialogs: List[Dialog] = []
        """A list of all dialogs"""

        self._intent_to_dialog: Dict[str, str] = {}
        """A mapping from intent id to dialog id"""

        self._load_dialogs()

        self.__active_dialog_stack = []
        """A stack of active (not finished) dialogs."""

    def _load_dialogs(self) -> None:
        """
        Load all dialogs of the bot.
        """
        self._dialogs = [
            NotUnderstanding(self._bot),
            QnA(self._bot),
            Clock(self._bot),
            News(self._bot),
            Cleanup(self._bot),
            QnAAnswer(self._bot),
            Choose(self._bot)
        ]

        self._intent_to_dialog = {
            "None".lower(): NotUnderstanding.ID,
            "QnA".lower(): QnA.ID,
            "Clock".lower(): Clock.ID,
            "News".lower(): News.ID,
            "Cleanup".lower(): Cleanup.ID,
            "Answer".lower(): QnAAnswer.ID,
            "Choose".lower(): Choose.ID
        }

    def __lookup_dialog(self, dialog_id: str) -> Optional[Dialog]:
        """
        Lookup a dialog by dialog id.

        :param dialog_id: the dialog id
        :return: the dialog iff found
        """
        return next((d for d in self._dialogs if d.dialog_id == dialog_id), None)

    async def handle(self, message: Message) -> None:
        """
        Handle a message by the dialog system.

        :param message: the message
        """
        (intents, entities) = self._bot.nlu.recognize(message.clean_content)

        await self._send_debug(message, intents, entities)

        if len(self.__active_dialog_stack) != 0:
            dialog = self.__active_dialog_stack.pop(0)
        elif intents is None or len(intents) == 0:
            dialog = NotUnderstanding.ID
        else:
            intent = intents[0].name
            score = intents[0].score
            dialog = self._intent_to_dialog.get(intent)

            if score <= self._bot.config.nlu_threshold:
                dialog = NotUnderstanding.ID
            elif intent == "QnA-Tasks":
                # Send Help Message for Tasks intent ..
                await send_help_message(message, self._bot)
                return
            elif intent.startswith("QnA"):
                dialog = QnA.ID

        dialog = self.__lookup_dialog(dialog)
        if dialog is None:
            await send(message.author, message.channel, self._bot, "Dialog nicht gefunden. Bitte an Bot-Admin wenden!")
            return

        result = await dialog.proceed(message, intents, entities)
        if result == DialogResult.WAIT_FOR_INPUT:
            self.__active_dialog_stack.insert(0, dialog.dialog_id)

    def has_active_dialog(self) -> bool:
        """
        Get the indicator whether a dialog is unfinished (active)

        :return: the indicator for an active dialog
        """
        return len(self.__active_dialog_stack) != 0

    async def _send_debug(self, message: Message, intents: List[IntentResult],
                          entities: List[EntityResult]) -> None:
        """
        Send debug information iff debug flag is enabled.

        :param message: the message to react to
        :param intents: the found intents
        :param entities: the found entities
        """
        if not self._bot.config.is_debug():
            return

        result: str = "------------\n"
        result += f"Intents({len(intents)}):\n"

        for intent in intents:
            result += f"{intent}\n"

        result += f"\nEntities({len(entities)}):\n"
        for entity in entities:
            result += f"{entity}\n"

        result += "------------"

        await send(message.author, message.channel, self._bot, result, mention=False)


class DeltaBot(BotBase):
    """ The DeltaBot main client. """

    def __init__(self) -> None:
        """ Initialize the DeltaBot. """
        super().__init__()
        self._methods = MethodVersionStore()

        self._user_to_instance = {}
        self._user_to_instance_lock = Lock()
        init_user_commands(self)
        self._discord_components = None

    async def on_ready(self) -> None:
        """ Will be executed on ready event. """
        print('Logged on as', self.user)
        activity = Activity(type=ActivityType.listening, name=f"{self.config.user_command_symbol}help")
        await self.change_presence(status=Status.online, activity=activity)

        print('Starting scheduler ..')
        self.scheduler.start_scheduler()

        # Register Discord Components and preserve on_socket_response
        self._methods.store_method(self.on_socket_response)
        self._discord_components = DiscordComponents(self)
        self._methods.store_method(self.on_socket_response)
        self._create_on_socket_response()

    def _create_on_socket_response(self):
        methods = self._methods.get_methods("on_socket_response")

        async def on_socket_response(payload: dict):
            for m in methods:
                try:
                    await m(payload)
                except Exception as e:
                    print(e)

        self.on_socket_response = on_socket_response

    async def on_socket_response(self, payload: dict):
        if self.config.is_debug():
            print(f"DEBUG (SOCKET): {payload}")

    async def on_message(self, message: Message) -> None:
        """
        Handle a new message.

        :param message: the message to handle
        """

        # don't respond to ourselves or bots
        if message.author == self.user or message.author.bot:
            return

        if await handle_system(self, message):
            self.log(message)
            return

        if await handle_user(self, message):
            self.log(message)
            return

        if await self._handle_unknown_command_symbols(message):
            self.log(message)
            return

        instance = self.__get_bot_instance(message.author)
        ch_id = message.channel.id

        handle_message: bool = False
        handle_message |= is_direct(message)
        handle_message |= self.user in message.mentions and ch_id in self.config.get_channels()
        handle_message |= instance.has_active_dialog() and ch_id in self.config.get_channels()

        if not handle_message:
            return

        await delete(message, self)
        self.log(message)

        if self.config.is_nlu_disabled():
            await send(message.author, message.channel, self,
                       f"NLU Einheit wurde vom Admin deaktiviert. Ich kann Deine Aussage daher nicht interpretieren :(")
            return

        await instance.handle(message)

    async def on_raw_select_option(self, payload: dict):
        """
        Handle a button click to a message.

        :param payload: the raw payload from discord
        """
        cid = payload["message"]["channel_id"]
        mid = payload["message"]["id"]
        selection_id = payload["data"]["custom_id"]
        selections = payload["data"]["values"]
        user_id = payload["member"]["user"]["id"]

        if user_id == self.user.id:
            return

        channel: TextChannel = await self.fetch_channel(cid)
        message: Message = await channel.fetch_message(mid)
        if message.author != self.user:
            return

        user = await self.fetch_user(user_id)
        await self._discord_component_response(selection_id, message, user, payload)

        if await handle_user_selection(self, payload, message, selection_id, selections, user_id):
            return

    async def on_raw_button_click(self, payload: dict) -> None:
        """
        Handle a button click to a message.

        :param payload: the raw payload from discord
        """
        cid = payload["message"]["channel_id"]
        mid = payload["message"]["id"]
        button_id = payload["data"]["custom_id"]
        user_id = payload["member"]["user"]["id"]

        if user_id == self.user.id:
            return

        channel: TextChannel = await self.fetch_channel(cid)
        message: Message = await channel.fetch_message(mid)

        if message.author != self.user:
            return

        user = await self.fetch_user(user_id)
        await self._discord_component_response(button_id, message, user, payload)

        if await handle_user_button(self, payload, message, button_id, user_id):
            return

    async def _discord_component_response(self, component_id: str, message: Message, user: User, payload: dict) -> None:
        """
        Generate a response that discord will not show an error to the user due to a timeout.

        :param component_id: the id of the component that has been pressed
        :param message: the associated message
        :param user: the user that has pressed the button
        :param payload: the raw payload of the event
        """
        component: Component = next((e for e in get_components(message.components) if e.id == component_id), None)
        if component is None:
            return

        interaction = Interaction(bot=self, client=self._discord_components, user=user, guild=get_guild(message),
                                  channel=message.channel, interacted_component=component, parent_component=component,
                                  raw_data={"d": payload}, message=message)
        await interaction.respond(type=InteractionType.DeferredUpdateMessage)

    async def _handle_unknown_command_symbols(self, message: Message):
        command_starts = ["!", "/", "\\", "-", "~", "$", "??", "=", "?"]
        command_starts.remove(self.config.system_command_symbol)
        command_starts.remove(self.config.user_command_symbol)

        if not any([message.content.strip().startswith(c) for c in command_starts]):
            return False

        user: User = message.author

        text = f"JFYI: Benutzerbefehle starten f??r mich mit `{self.config.user_command_symbol}`"

        # Check whether messages already contain explanations
        async for m in user.history(limit=20):
            if m.author != self.user:
                continue
            if m.content == text:
                return True

        await user.send(text)
        return True

    def __get_bot_instance(self, author: User) -> BotInstance:
        """
        Get the user's bot instance

        :param author: the user
        :return: the bot instance of the user
        """
        with self._user_to_instance_lock:
            instance = self._user_to_instance.get(author.id)
            if instance is None:
                instance = BotInstance(self)
                self._user_to_instance[author.id] = instance
        return instance


def start() -> None:
    """The main method of the system."""
    discord = DeltaBot()
    discord.run(environ["DiscordToken"])


if __name__ == "__main__":
    # Start the Bot
    start()
