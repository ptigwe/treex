import functools
import threading
import typing as tp
from contextlib import contextmanager
from dataclasses import dataclass

import jax
import jax.numpy as jnp
import jax.tree_util
import numpy as np
import treeo as to
from rich.table import Table
from rich.text import Text
from treeo import tree as tree_module

from treex import types, utils
from treex.treex import Filters, Treex

A = tp.TypeVar("A")
B = tp.TypeVar("B")
M = tp.TypeVar("M", bound="Module")
Filter = tp.Union[
    tp.Type[tp.Type[tp.Any]],
    tp.Callable[[to.FieldInfo], bool],
]


@dataclass
class _Context(threading.local):
    call_info: tp.Optional[tp.Dict["Module", tp.Tuple[types.Inputs, tp.Any]]] = None

    def __enter__(self):
        global _CONTEXT
        self._old_context = _CONTEXT
        _CONTEXT = self

    def __exit__(self, *args):
        global _CONTEXT
        _CONTEXT = self._old_context

    @contextmanager
    def update(self, **kwargs):
        fields = vars(self).copy()
        fields.pop("_old_context", None)
        fields.update(kwargs)

        with _Context(**fields):
            yield


@dataclass
class _InitContext(threading.local):
    key: tp.Optional[jnp.ndarray] = None
    initializing: bool = False

    def __enter__(self):
        global _INIT_CONTEXT
        self._old_context = _INIT_CONTEXT
        _INIT_CONTEXT = self

    def __exit__(self, *args):
        global _INIT_CONTEXT
        _INIT_CONTEXT = self._old_context

    @contextmanager
    def update(self, **kwargs):
        fields = vars(self).copy()
        fields.pop("_old_context", None)
        fields.update(kwargs)

        with _InitContext(**fields):
            yield


_CONTEXT = _Context()
_INIT_CONTEXT = _InitContext()

# -----------------------------------------
# Module
# -----------------------------------------
class ModuleMeta(to.TreeMeta):
    def construct(cls, obj: M, *args, **kwargs) -> M:
        # reset context during construction
        with _InitContext():
            obj = super().construct(obj, *args, **kwargs)

        if tree_module._COMPACT_CONTEXT.in_compact:
            if _INIT_CONTEXT.key is None:
                raise RuntimeError(
                    f"Trying to construct new module {obj} with a compact context outside of `init` or an `rng_key` context."
                )

            if not obj.initialized:
                obj.init(key=next_key(), inplace=True, _set_initialize=False)

        return obj


class Module(Treex, Filters, metaclass=ModuleMeta):
    # use to.field to copy class vars to instance
    _training: bool = to.static(True)
    _initialized: bool = to.static(False)
    _frozen: bool = to.static(False)

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def training(self) -> bool:
        return self._training

    @property
    def frozen(self) -> bool:
        return self._frozen

    def __init_subclass__(cls):
        if issubclass(cls, tp.Callable):
            orig_call = cls.__call__

            @functools.wraps(cls.__call__)
            def new_call(self: Module, *args, **kwargs):
                outputs = orig_call(self, *args, **kwargs)

                if _CONTEXT.call_info is not None and self not in _CONTEXT.call_info:
                    inputs = types.Inputs(*args, **kwargs)
                    _CONTEXT.call_info[self] = (inputs, outputs)

                return outputs

            cls.__call__ = new_call

        return super().__init_subclass__()

    def init(
        self: M,
        key: tp.Union[int, jnp.ndarray],
        inputs: types.InputLike = to.MISSING,
        call_method: str = "__call__",
        *,
        inplace: bool = False,
        _set_initialize: bool = True,
    ) -> M:
        """
        Method version of `tx.init`, it applies `self` as first argument.

        `init` creates a new module with the same structure, but with its fields initialized given a seed `key`. The following
        procedure is used:

        1. The input `key` is split and iteratively updated before passing a derived value to any
            process that requires initialization.
        2. `Initializer`s are called and applied to the module first.
        3. `Module.rng_init` methods are called last.

        Arguments:
            key: The seed to use for initialization.
        Returns:
            The new module with the fields initialized.
        """
        module = self.copy() if not inplace else self
        key = utils.Key(key)

        with _INIT_CONTEXT.update(key=key, initializing=True):

            module: M = module.map(
                lambda initializer: (
                    initializer(next_key())
                    if isinstance(initializer, types.Initializer)
                    else initializer
                ),
                is_leaf=lambda x: isinstance(x, types.Initializer),
                inplace=True,
            )

            def call_rng_init(module: Module):
                if isinstance(module, Module) and not module._initialized:
                    module.rng_init()

            module = to.apply(call_rng_init, module, inplace=True)

            if inputs is not to.MISSING:
                inputs = types.Inputs.from_value(inputs)
                method = getattr(module, call_method)
                method(*inputs.args, **inputs.kwargs)

        if _set_initialize:

            def set_initialized(module: Module):
                if isinstance(module, Module) and not module._initialized:
                    module._initialized = True

            module = to.apply(set_initialized, module, inplace=True)

        return module

    def rng_init(self) -> None:
        pass

    def tabulate(
        self,
        inputs: tp.Union[types.InputLike, to.Missing] = to.MISSING,
        depth: int = -1,
        signature: bool = False,
        param_types: bool = True,
    ) -> str:
        """
        Returns a tabular representation of the module.

        Arguments:
            depth: The maximum depth of the representation in terms of nested Modules, -1 means no limit.
            signature: Whether to show the signature of the Module.
            param_types: Whether to show the types of the parameters.
        Returns:
            A string containing the tabular representation.
        """
        self = to.copy(self)

        if inputs is not to.MISSING:
            inputs = types.Inputs.from_value(inputs)

            if not isinstance(self, tp.Callable):
                raise TypeError(
                    "`inputs` can only be specified if the module is a callable."
                )

            with _Context(call_info={}):

                # call using self to preserve references
                def eval_call(args, kwargs):
                    assert isinstance(self, tp.Callable)
                    return self(*args, **kwargs)

                jax.eval_shape(
                    eval_call,
                    inputs.args,
                    inputs.kwargs,
                )
                call_info = _CONTEXT.call_info

        else:
            call_info = None

        with to.add_field_info():
            flat: tp.List[to.FieldInfo]
            flat, _ = jax.tree_flatten(self)
            tree_part_types: tp.Tuple[tp.Type[types.TreePart], ...] = tuple(
                {
                    field_info.kind
                    for field_info in flat
                    if utils._generic_issubclass(field_info.kind, types.TreePart)
                }
            )

        path = ()
        rows = list(
            utils._get_tabulate_rows(
                path, self, depth, tree_part_types, signature, param_types
            )
        )

        modules = [row[0] for row in rows]
        rows = [row[1:] for row in rows]

        if call_info is not None:
            for module, row in zip(modules, rows):
                if module in call_info:
                    inputs, outputs = call_info[module]
                    simplified_inputs = (
                        inputs.args[0]
                        if len(inputs.kwargs) == 0 and len(inputs.args) == 1
                        else inputs.kwargs
                        if len(inputs.kwargs) == 0
                        else inputs.kwargs
                        if len(inputs.args) == 0
                        else (inputs.args, inputs.kwargs)
                    )

                    inputs_repr = utils._format_param_tree(simplified_inputs)
                    outputs_repr = utils._format_param_tree(outputs)
                else:
                    inputs_repr = ""
                    outputs_repr = ""

                row.insert(3, outputs_repr)
                row.insert(3, inputs_repr)

        n_non_treepart_cols = 2 if call_info is None else 4

        rows[0][0] = "*"
        rows.append(
            [""] * n_non_treepart_cols
            + ["Total:"]
            + [
                utils._format_obj_size(self.filter(kind), add_padding=True)
                for kind in tree_part_types
            ]
        )
        utils._add_padding(rows)

        table = Table(
            show_header=True,
            show_lines=True,
            show_footer=True,
            # box=rich.box.HORIZONTALS,
        )

        table.add_column("path")
        table.add_column("module")
        table.add_column("params")

        if call_info is not None:
            table.add_column("inputs")
            table.add_column("outputs")

        for tree_part_type in tree_part_types:
            type_name = tree_part_type.__name__
            if type_name.startswith("_"):
                type_name = type_name[1:]

            table.add_column(type_name)

        for row in rows[:-1]:
            table.add_row(*row)

        table.columns[n_non_treepart_cols].footer = Text.from_markup(
            rows[-1][n_non_treepart_cols], justify="right"
        )

        for i in range(len(tree_part_types)):
            table.columns[n_non_treepart_cols + 1 + i].footer = rows[-1][
                n_non_treepart_cols + 1 + i
            ]

        table.caption_style = "bold"
        table.caption = "\nTotal Parameters: " + utils._format_obj_size(
            self, add_padding=False
        )

        return utils._get_rich_repr(table)


def next_key() -> jnp.ndarray:
    """
    Returns the next key.

    Returns:
        The next key.
    """
    if _INIT_CONTEXT.key is None:
        raise RuntimeError(
            "RNG key not set, you are either calling an uninitialized Module outside `.init` or forgot to call `rng_key` context manager."
        )

    key, _INIT_CONTEXT.key = utils.iter_split(_INIT_CONTEXT.key)
    return key


@contextmanager
def rng_key(key: types.KeyLike):
    key = utils.Key(key)

    with _INIT_CONTEXT.update(key=key):
        yield
