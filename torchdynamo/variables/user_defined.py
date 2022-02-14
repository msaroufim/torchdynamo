import collections
import inspect
import types
from typing import Dict
from typing import List

from .. import variables
from ..guards import Guard
from ..guards import GuardBuilder
from ..source import ODictGetItemSource
from ..utils import is_namedtuple_cls
from ..utils import namedtuple_fields
from .base import VariableTracker


class UserDefinedClassVariable(VariableTracker):
    def __init__(self, value, **kwargs):
        super().__init__(**kwargs)
        self.value = value

    def as_python_constant(self):
        return self.value

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        if is_namedtuple_cls(self.value):
            fields = namedtuple_fields(self.value)
            items = list(args)
            items.extend([None] * (len(fields) - len(items)))
            for name, value in kwargs.items():
                assert name in fields
                items[fields.index(name)] = value
            assert all(x is not None for x in items)
            return variables.NamedTupleVariable(
                items, self.value, **VariableTracker.propagate(self, items)
            )
        return super().call_function(tx, args, kwargs)


class UserDefinedObjectVariable(VariableTracker):
    """
    Mostly objects of defined type.  Catch-all for something where we only know the type.
    """

    def __init__(self, value, value_type=None, **kwargs):
        super(UserDefinedObjectVariable, self).__init__(**kwargs)
        self.value = value
        self.value_type = value_type or type(value)

    def __str__(self):
        inner = self.value_type.__name__
        if inner == "builtin_function_or_method":
            inner = str(getattr(self.value, "__name__", None))
        return f"{self.__class__.__name__}({inner})"

    def python_type(self):
        return self.value_type

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        from . import ConstantVariable
        from . import TupleVariable
        from . import UserMethodVariable

        options = VariableTracker.propagate(self, args, kwargs.values())
        if name not in getattr(self.value, "__dict__", {}):
            try:
                method = inspect.getattr_static(type(self.value), name)
            except AttributeError:
                method = None

            if method is collections.OrderedDict.keys and self.source:
                # subclass of OrderedDict
                assert not (args or kwargs)
                keys = list(self.value.keys())
                assert all(map(ConstantVariable.is_literal, keys))
                return TupleVariable(
                    [ConstantVariable(k, **options) for k in keys], **options
                ).add_guard(
                    Guard(
                        self.source.name(),
                        self.source.guard_source(),
                        GuardBuilder.ODICT_KEYS,
                    )
                )

            if (
                method is collections.OrderedDict.items
                and isinstance(self.value, collections.OrderedDict)
                and self.source
            ):
                assert not (args or kwargs)
                items = []
                keys = self.call_method(tx, "keys", [], {})
                options = VariableTracker.propagate(self, args, kwargs.values(), keys)
                for key in keys.unpack_var_sequence(tx):
                    items.append(
                        TupleVariable(
                            [key, self.odict_getitem(tx, key)],
                            **options,
                        )
                    )
                return TupleVariable(items, **options)

            if method is collections.OrderedDict.__getitem__ and len(args) == 1:
                assert not kwargs
                return self.odict_getitem(tx, args[0])

            # check for methods implemented in C++
            if isinstance(method, types.FunctionType):
                # TODO(jansel): add a guard to check for monkey patching?
                return UserMethodVariable(method, self, **options).call_function(
                    tx, args, kwargs
                )

        return super().call_method(tx, name, args, kwargs)

    def odict_getitem(self, tx, key):
        from .builder import VariableBuilder

        return VariableBuilder(
            tx,
            ODictGetItemSource(self.source, key.as_python_constant()),
        )(
            collections.OrderedDict.__getitem__(self.value, key.as_python_constant())
        ).add_options(
            key, self
        )
