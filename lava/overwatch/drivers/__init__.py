from lava.overwatch.interfaces import (
    IOverwatchDriver,
    IOverwatchDriverInterface
)
from simplejson import loads
import inspect


class BaseOverwatchDriver(IOverwatchDriver):
    """
    Base driver that simplifies the implementation of the IOverwatchDriver
    subclasses. The only thing that should be implemented is _get_interfaces().

    The configuration string stored in the overwatch.Device model *MUST* be a
    JSON string. 
    """

    def __init__(self, config):
        if config != "":
            self._config = loads(config)
        else:
            self._config = None

    def _get_interfaces(self):
        """
        Return a dictionary mapping interfaces to callables. The callable takes
        the form of ``lambda driver: interface``, that is, given the instance of
        the driver it must return the correct interface. In simple cases it can
        just return the driver object itself.
        """
        raise NotImplementedError()

    def enumerate_interfaces(self):
        return self._get_interfaces().iterkeys()

    def get_interface(self, name):
        try:
            callback = self._get_interfaces()[name]
            return callback(self)
        except KeyError:
            raise ValueError("Interface %r is not implemented by this "
                             "driver" % name)


def is_action(obj):
    return inspect.ismethod(obj) and getattr(obj, "is_action", None) == True


class BaseOverwatchInterface(IOverwatchDriverInterface):

    def get_name(self):
        return self.INTERFACE_NAME

    def enumerate_actions(self):
        for name, member in inspect.getmembers(self, is_action):
            yield name

    def run_action(self, name, **params):
        impl = getattr(self, name)
        if not is_action(impl):
            raise ValueError(
                "Action %r is not provided by this interface" % (name))
        impl(**params)
