import collections
import dataclasses
import functools
import importlib
import inspect
import random
import types
from typing import Dict
from typing import List

import torch.nn

from .. import variables
from ..exc import unimplemented
from ..guards import Guard
from ..guards import GuardBuilder
from ..source import AttrSource
from ..source import ODictGetItemSource
from ..source import RandomValueSource
from ..utils import is_namedtuple_cls
from ..utils import namedtuple_fields
from .base import MutableLocal
from .base import VariableTracker


class UserDefinedVariable(VariableTracker):
    pass


class UserDefinedClassVariable(UserDefinedVariable):
    def __init__(self, value, **kwargs):
        super().__init__(**kwargs)
        self.value = value

    def as_python_constant(self):
        return self.value

    def var_getattr(self, tx, name: str) -> "VariableTracker":
        options = VariableTracker.propagate(self)
        try:
            obj = inspect.getattr_static(self.value, name)
        except AttributeError:
            obj = None

        if isinstance(obj, staticmethod):
            return variables.UserFunctionVariable(obj.__get__(self.value), **options)
        elif isinstance(obj, classmethod):
            return variables.UserMethodVariable(obj.__func__, self, **options)

        return super(UserDefinedClassVariable, self).var_getattr(tx, name)

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        if (
            name == "__subclasses__"
            and len(args) == 0
            and not kwargs
            and "__subclasses__" not in self.value.__dict__
        ):
            options = VariableTracker.propagate(self, args, kwargs.values())
            options["mutable_local"] = MutableLocal()
            subs_as_vars: List[VariableTracker] = list()
            for sub in self.value.__subclasses__():
                source = AttrSource(tx.import_source(sub.__module__), sub.__name__)
                subs_as_vars.append(
                    variables.UserDefinedClassVariable(sub, source=source)
                )

            return variables.ListVariable(subs_as_vars, **options)

        return super().call_method(tx, args, kwargs)

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        from ..side_effects import SideEffects

        options = VariableTracker.propagate(self, args, kwargs.values())

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
        elif (
            inspect.getattr_static(self.value, "__new__", None) in (object.__new__,)
            and SideEffects.cls_supports_mutation_side_effects(self.value)
            and self.source
        ):
            var = tx.output.side_effects.track_object_new(
                self.source, self.value, UserDefinedObjectVariable, options
            )
            return var.add_options(var.call_method(tx, "__init__", args, kwargs))
        elif variables.DataClassVariable.is_matching_cls(self.value):
            options["mutable_local"] = MutableLocal()
            return variables.DataClassVariable.create(self.value, args, kwargs, options)

        return super().call_function(tx, args, kwargs)

    def const_getattr(self, tx, name):
        if name == "__name__":
            return self.value.__name__
        return super().const_getattr(tx, name)


class UserDefinedObjectVariable(UserDefinedVariable):
    """
    Mostly objects of defined type.  Catch-all for something where we only know the type.
    """

    def __init__(self, value, value_type=None, **kwargs):
        super(UserDefinedObjectVariable, self).__init__(**kwargs)
        self.value = value
        self.value_type = value_type or type(value)
        assert type(value) is self.value_type

    def __str__(self):
        inner = self.value_type.__name__
        if inner == "builtin_function_or_method":
            inner = str(getattr(self.value, "__name__", None))
        return f"{self.__class__.__name__}({inner})"

    def python_type(self):
        return self.value_type

    @staticmethod
    @functools.lru_cache(None)
    def _supported_random_functions():
        # func : example value
        fns = {
            random.random: 0.5,
            random.randint: 10,
            random.randrange: 20,
            random.uniform: 1.5,
        }
        return fns

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

            if method is object.__init__:
                return ConstantVariable(None, **options)

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

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        from .builder import VariableBuilder

        if (
            self.value in self._supported_random_functions()
            and all(k.is_python_constant() for k in args)
            and all(v.is_python_constant() for v in kwargs.values())
        ):
            example_value = self._supported_random_functions()[self.value]
            source = RandomValueSource(random_call_index=len(tx.random_calls))
            tx.random_calls.append(
                (
                    self.value,
                    [x.as_python_constant() for x in args],
                    {k: v.as_python_constant() for k, v in kwargs.items()},
                )
            )
            return VariableBuilder(tx, source).wrap_unspecialized_primitive(
                example_value
            )

        return super().call_function(tx, args, kwargs)

    def _check_for_getattribute(self):
        try:
            if isinstance(
                inspect.getattr_static(type(self.value), "__getattribute__"),
                types.FunctionType,
            ):
                unimplemented("UserDefinedObjectVariable with custom __getattribute__")
        except AttributeError:
            pass

    def _check_for_getattr(self):
        try:
            getattr_fn = inspect.getattr_static(type(self.value), "__getattr__")
        except AttributeError:
            getattr_fn = None
        if getattr_fn is torch.nn.Module.__getattr__:
            # ignore this case of getattr
            getattr_fn = None
        return getattr_fn

    def _getattr_static(self, name):
        if isinstance(self.value, (dataclasses.Field, torch.nn.Module)):
            # getattr_static doesn't work on these
            subobj = getattr(self.value, name)
        else:
            subobj = inspect.getattr_static(self.value, name)
        return subobj

    def var_getattr(self, tx, name):
        from . import ConstantVariable
        from .builder import VariableBuilder

        options = VariableTracker.propagate(self)
        value = self.value
        source = AttrSource(self.source, name) if self.source else None
        self._check_for_getattribute()
        getattr_fn = self._check_for_getattr()

        try:
            subobj = self._getattr_static(name)
        except AttributeError:
            if isinstance(getattr_fn, types.FunctionType):
                return variables.UserMethodVariable(
                    getattr_fn, self, **options
                ).call_function(tx, [ConstantVariable(name)], {})
            elif getattr_fn is not None:
                unimplemented("UserDefined with non-function __getattr__")

        if isinstance(subobj, property):
            return variables.UserMethodVariable(
                subobj.fget, self, **options
            ).call_function(tx, [], {})

        if (
            name in getattr(value, "__dict__", {})
            or ConstantVariable.is_literal(subobj)
            or isinstance(
                subobj,
                (
                    torch.Tensor,
                    torch.nn.Module,
                ),
            )
        ):
            if source:
                return VariableBuilder(tx, source)(subobj).add_options(options)
            elif ConstantVariable.is_literal(subobj):
                return ConstantVariable(subobj, **options)

        if (
            name not in getattr(value, "__dict__", {})
            and type(value).__module__.startswith("torch.")
            and not callable(value)
        ):
            if not source:
                assert getattr(
                    importlib.import_module(type(value).__module__),
                    type(value).__name__,
                ) is type(value)
                source = AttrSource(
                    AttrSource(
                        tx.import_source(type(value).__module__), type(value).__name__
                    ),
                    name,
                )
            return VariableBuilder(tx, source)(subobj).add_options(options)

        if isinstance(
            subobj,
            (
                torch.distributions.constraints._Interval,
                torch.distributions.constraints._Real,
                torch.distributions.constraints.Constraint,
            ),
        ):
            return UserDefinedObjectVariable(subobj, source=source, **options)

        if name == "__class__":
            return UserDefinedClassVariable(type(self.value), source=source, **options)

        return variables.GetAttrVariable(self, name, source=source, **options)

    def call_hasattr(self, tx, name: str) -> "VariableTracker":
        if not self.source:
            unimplemented("hasattr no source")
        options = VariableTracker.propagate(self)
        options["guards"].add(
            AttrSource(self.source, name).make_guard(GuardBuilder.HASATTR)
        )
        if self._check_for_getattribute() or self._check_for_getattr():
            unimplemented("hasattr with custom __getattr__")

        try:
            self._getattr_static(name)
            return variables.ConstantVariable(True, **options)
        except AttributeError:
            return variables.ConstantVariable(False, **options)

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
