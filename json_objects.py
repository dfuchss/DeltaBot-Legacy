def convert_to_dict(obj: object) -> dict:
    """
    A function takes in a custom object and returns a dictionary representation of the object.
    This dict representation includes meta data such as the object's module and class names.
    """

    #  Populate the dictionary with object meta data
    obj_dict = {
        "__class__": obj.__class__.__name__,
        "__module__": obj.__module__
    }

    #  Populate the dictionary with object properties
    obj_dict.update(obj.__dict__)

    return obj_dict


def dict_to_obj(our_dict: dict) -> object:
    """
    Function that takes in a dict and returns a custom object associated with the dict.
    This function makes use of the "__module__" and "__class__" metadata in the dictionary
    to know which object type to create.
    """
    if "__class__" in our_dict:
        # Pop ensures we remove metadata from the dict to leave only the instance arguments
        class_name = our_dict.pop("__class__")

        # Get the module name from the dict and import it
        module_name = our_dict.pop("__module__")

        # We use the built in __import__ function since the module name is not yet known at runtime
        module = __import__(module_name)

        # Get the class from the module
        class_ = getattr(module, class_name)

        # Use dictionary unpacking to initialize the object
        obj = class_(**our_dict)
    else:
        obj = our_dict
    return obj
