"""Helpers that store information about functions and the related classes."""

from __future__ import annotations

from mypy.nodes import FuncItem
from mypyc.ir.class_ir import ClassIR
from mypyc.ir.func_ir import INVALID_FUNC_DEF
from mypyc.ir.ops import BasicBlock, Value
from mypyc.irbuild.targets import AssignmentTarget


class FuncInfo:
    """Contains information about functions as they are generated."""

    def __init__(
        self,
        fitem: FuncItem = INVALID_FUNC_DEF,
        name: str = "",
        class_name: str | None = None,
        namespace: str = "",
        is_nested: bool = False,
        contains_nested: bool = False,
        is_decorated: bool = False,
        in_non_ext: bool = False,
        add_nested_funcs_to_env: bool = False,
    ) -> None:
        self.fitem = fitem
        self.name = name
        self.class_name = class_name
        self.ns = namespace
        # Callable classes implement the '__call__' method, and are used to represent functions
        # that are nested inside of other functions.
        self._callable_class: ImplicitClass | None = None
        # Environment classes are ClassIR instances that contain attributes representing the
        # variables in the environment of the function they correspond to. Environment classes are
        # generated for functions that contain nested functions.
        self._env_class: ClassIR | None = None
        # Generator classes implement the '__next__' method, and are used to represent generators
        # returned by generator functions.
        self._generator_class: GeneratorClass | None = None
        # Environment class registers are the local registers associated with instances of an
        # environment class, used for getting and setting attributes. curr_env_reg is the register
        # associated with the current environment.
        self._curr_env_reg: Value | None = None
        # These are flags denoting whether a given function is nested, contains a nested function,
        # is decorated, or is within a non-extension class.
        self.is_nested = is_nested
        self.contains_nested = contains_nested
        self.is_decorated = is_decorated
        self.in_non_ext = in_non_ext
        self.add_nested_funcs_to_env = add_nested_funcs_to_env

        # TODO: add field for ret_type: RType = none_rprimitive

    def namespaced_name(self) -> str:
        return "_".join(x for x in [self.name, self.class_name, self.ns] if x)

    @property
    def is_generator(self) -> bool:
        return self.fitem.is_generator or self.fitem.is_coroutine

    @property
    def is_coroutine(self) -> bool:
        return self.fitem.is_coroutine

    @property
    def callable_class(self) -> ImplicitClass:
        assert self._callable_class is not None
        return self._callable_class

    @callable_class.setter
    def callable_class(self, cls: ImplicitClass) -> None:
        self._callable_class = cls

    @property
    def env_class(self) -> ClassIR:
        assert self._env_class is not None
        return self._env_class

    @env_class.setter
    def env_class(self, ir: ClassIR) -> None:
        self._env_class = ir

    @property
    def generator_class(self) -> GeneratorClass:
        assert self._generator_class is not None
        return self._generator_class

    @generator_class.setter
    def generator_class(self, cls: GeneratorClass) -> None:
        self._generator_class = cls

    @property
    def curr_env_reg(self) -> Value:
        assert self._curr_env_reg is not None
        return self._curr_env_reg

    def can_merge_generator_and_env_classes(self) -> bool:
        # In simple cases we can place the environment into the generator class,
        # instead of having two separate classes.
        return self.is_generator and not self.is_nested and not self.contains_nested


class ImplicitClass:
    """Contains information regarding implicitly generated classes.

    Implicit classes are generated for nested functions and generator
    functions. They are not explicitly defined in the source code.

    NOTE: This is both a concrete class and used as a base class.
    """

    def __init__(self, ir: ClassIR) -> None:
        # The ClassIR instance associated with this class.
        self.ir = ir
        # The register associated with the 'self' instance for this generator class.
        self._self_reg: Value | None = None
        # Environment class registers are the local registers associated with instances of an
        # environment class, used for getting and setting attributes. curr_env_reg is the register
        # associated with the current environment. prev_env_reg is the self.__mypyc_env__ field
        # associated with the previous environment.
        self._curr_env_reg: Value | None = None
        self._prev_env_reg: Value | None = None

    @property
    def self_reg(self) -> Value:
        assert self._self_reg is not None
        return self._self_reg

    @self_reg.setter
    def self_reg(self, reg: Value) -> None:
        self._self_reg = reg

    @property
    def curr_env_reg(self) -> Value:
        assert self._curr_env_reg is not None
        return self._curr_env_reg

    @curr_env_reg.setter
    def curr_env_reg(self, reg: Value) -> None:
        self._curr_env_reg = reg

    @property
    def prev_env_reg(self) -> Value:
        assert self._prev_env_reg is not None
        return self._prev_env_reg

    @prev_env_reg.setter
    def prev_env_reg(self, reg: Value) -> None:
        self._prev_env_reg = reg


class GeneratorClass(ImplicitClass):
    """Contains information about implicit generator function classes."""

    def __init__(self, ir: ClassIR) -> None:
        super().__init__(ir)
        # This register holds the label number that the '__next__' function should go to the next
        # time it is called.
        self._next_label_reg: Value | None = None
        self._next_label_target: AssignmentTarget | None = None

        # These registers hold the error values for the generator object for the case that the
        # 'throw' function is called.
        self.exc_regs: tuple[Value, Value, Value] | None = None

        # Holds the arg passed to send
        self.send_arg_reg: Value | None = None

        # Holds the PyObject ** pointer through which return value can be passed
        # instead of raising StopIteration(ret_value) (only if not NULL). This
        # is used for faster native-to-native calls.
        self.stop_iter_value_reg: Value | None = None

        # The switch block is used to decide which instruction to go using the value held in the
        # next-label register.
        self.switch_block = BasicBlock()
        self.continuation_blocks: list[BasicBlock] = []

    @property
    def next_label_reg(self) -> Value:
        assert self._next_label_reg is not None
        return self._next_label_reg

    @next_label_reg.setter
    def next_label_reg(self, reg: Value) -> None:
        self._next_label_reg = reg

    @property
    def next_label_target(self) -> AssignmentTarget:
        assert self._next_label_target is not None
        return self._next_label_target

    @next_label_target.setter
    def next_label_target(self, target: AssignmentTarget) -> None:
        self._next_label_target = target
