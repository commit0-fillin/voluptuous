from __future__ import annotations
import collections
import inspect
import itertools
import re
import sys
import typing
from collections.abc import Generator
from contextlib import contextmanager
from functools import cache, wraps
from voluptuous import error as er
from voluptuous.error import Error
PREVENT_EXTRA = 0
ALLOW_EXTRA = 1
REMOVE_EXTRA = 2

class Undefined(object):

    def __nonzero__(self):
        return False

    def __repr__(self):
        return '...'
UNDEFINED = Undefined()
DefaultFactory = typing.Union[Undefined, typing.Callable[[], typing.Any]]

def Extra(_) -> None:
    """Allow keys in the data that are not present in the schema."""
    return lambda _: True
extra = Extra
primitive_types = (bool, bytes, int, str, float, complex)
Schemable = typing.Union['Schema', 'Object', collections.abc.Mapping, list, tuple, frozenset, set, bool, bytes, int, str, float, complex, type, object, dict, None, typing.Callable]

class Schema(object):
    """A validation schema.

    The schema is a Python tree-like structure where nodes are pattern
    matched against corresponding trees of values.

    Nodes can be values, in which case a direct comparison is used, types,
    in which case an isinstance() check is performed, or callables, which will
    validate and optionally convert the value.

    We can equate schemas also.

    For Example:

            >>> v = Schema({Required('a'): str})
            >>> v1 = Schema({Required('a'): str})
            >>> v2 = Schema({Required('b'): str})
            >>> assert v == v1
            >>> assert v != v2

    """
    _extra_to_name = {REMOVE_EXTRA: 'REMOVE_EXTRA', ALLOW_EXTRA: 'ALLOW_EXTRA', PREVENT_EXTRA: 'PREVENT_EXTRA'}

    def __init__(self, schema: Schemable, required: bool=False, extra: int=PREVENT_EXTRA) -> None:
        """Create a new Schema.

        :param schema: Validation schema. See :module:`voluptuous` for details.
        :param required: Keys defined in the schema must be in the data.
        :param extra: Specify how extra keys in the data are treated:
            - :const:`~voluptuous.PREVENT_EXTRA`: to disallow any undefined
              extra keys (raise ``Invalid``).
            - :const:`~voluptuous.ALLOW_EXTRA`: to include undefined extra
              keys in the output.
            - :const:`~voluptuous.REMOVE_EXTRA`: to exclude undefined extra keys
              from the output.
            - Any value other than the above defaults to
              :const:`~voluptuous.PREVENT_EXTRA`
        """
        self.schema: typing.Any = schema
        self.required = required
        self.extra = int(extra)
        self._compiled = self._compile(schema)

    @classmethod
    def infer(cls, data, **kwargs) -> Schema:
        """Create a Schema from concrete data (e.g. an API response).

        For example, this will take a dict like:

        {
            'foo': 1,
            'bar': {
                'a': True,
                'b': False
            },
            'baz': ['purple', 'monkey', 'dishwasher']
        }

        And return a Schema:

        {
            'foo': int,
            'bar': {
                'a': bool,
                'b': bool
            },
            'baz': [str]
        }

        Note: only very basic inference is supported.
        """
        if isinstance(data, dict):
            return cls({k: cls.infer(v) for k, v in data.items()})
        elif isinstance(data, list):
            return [cls.infer(data[0])] if data else list
        elif isinstance(data, tuple):
            return tuple(cls.infer(item) for item in data)
        elif isinstance(data, set):
            return {cls.infer(next(iter(data)))} if data else set
        else:
            return type(data)

    def __eq__(self, other):
        if not isinstance(other, Schema):
            return False
        return other.schema == self.schema

    def __ne__(self, other):
        return not self == other

    def __str__(self):
        return str(self.schema)

    def __repr__(self):
        return '<Schema(%s, extra=%s, required=%s) object at 0x%x>' % (self.schema, self._extra_to_name.get(self.extra, '??'), self.required, id(self))

    def __call__(self, data):
        """Validate data against this schema."""
        try:
            return self._compiled([], data)
        except er.MultipleInvalid:
            raise
        except er.Invalid as e:
            raise er.MultipleInvalid([e])

    def _compile_mapping(self, schema, invalid_msg=None):
        """Create validator for given mapping."""
        invalid_msg = invalid_msg or 'mapping value'

        def validate_mapping(path, iterable, out):
            if not isinstance(iterable, collections.abc.Mapping):
                raise er.DictInvalid(invalid_msg)

            for key, value in iterable.items():
                schema_key = key
                if key not in schema:
                    if isinstance(key, str):
                        schema_key = next((k for k in schema if isinstance(k, str) and k.lower() == key.lower()), None)
                    if schema_key is None:
                        if self.extra == PREVENT_EXTRA:
                            raise er.Invalid(f'extra keys not allowed @ data[{key!r}]')
                        if self.extra == REMOVE_EXTRA:
                            continue
                try:
                    out[key] = schema[schema_key](path + [key], value)
                except er.Invalid as e:
                    raise er.MultipleInvalid([e])

            if self.required:
                for key in schema:
                    if isinstance(key, Required) and key not in iterable:
                        raise er.RequiredFieldInvalid(f'required key not provided @ data[{key!r}]')

            return out

        return validate_mapping

    def _compile_object(self, schema):
        """Validate an object.

        Has the same behavior as dictionary validator but work with object
        attributes.

        For example:

            >>> class Structure(object):
            ...     def __init__(self, one=None, three=None):
            ...         self.one = one
            ...         self.three = three
            ...
            >>> validate = Schema(Object({'one': 'two', 'three': 'four'}, cls=Structure))
            >>> with raises(er.MultipleInvalid, "not a valid value for object value @ data['one']"):
            ...   validate(Structure(one='three'))

        """
        def validate_object(path, data):
            if not isinstance(data, schema.cls):
                raise er.ObjectInvalid('expected {} but got {}'.format(schema.cls.__name__, type(data).__name__))
            
            errors = []
            for key, validator in schema.items():
                if hasattr(data, key):
                    try:
                        setattr(data, key, validator(getattr(data, key)))
                    except er.Invalid as e:
                        errors.append(e)
            
            if errors:
                raise er.MultipleInvalid(errors)
            
            return data

        return validate_object

    def _compile_dict(self, schema):
        """Validate a dictionary.

        A dictionary schema can contain a set of values, or at most one
        validator function/type.

        A dictionary schema will only validate a dictionary:

            >>> validate = Schema({})
            >>> with raises(er.MultipleInvalid, 'expected a dictionary'):
            ...   validate([])

        An invalid dictionary value:

            >>> validate = Schema({'one': 'two', 'three': 'four'})
            >>> with raises(er.MultipleInvalid, "not a valid value for dictionary value @ data['one']"):
            ...   validate({'one': 'three'})

        An invalid key:

            >>> with raises(er.MultipleInvalid, "extra keys not allowed @ data['two']"):
            ...   validate({'two': 'three'})


        Validation function, in this case the "int" type:

            >>> validate = Schema({'one': 'two', 'three': 'four', int: str})

        Valid integer input:

            >>> validate({10: 'twenty'})
            {10: 'twenty'}

        By default, a "type" in the schema (in this case "int") will be used
        purely to validate that the corresponding value is of that type. It
        will not Coerce the value:

            >>> with raises(er.MultipleInvalid, "extra keys not allowed @ data['10']"):
            ...   validate({'10': 'twenty'})

        Wrap them in the Coerce() function to achieve this:
            >>> from voluptuous import Coerce
            >>> validate = Schema({'one': 'two', 'three': 'four',
            ...                    Coerce(int): str})
            >>> validate({'10': 'twenty'})
            {10: 'twenty'}

        Custom message for required key

            >>> validate = Schema({Required('one', 'required'): 'two'})
            >>> with raises(er.MultipleInvalid, "required @ data['one']"):
            ...   validate({})

        (This is to avoid unexpected surprises.)

        Multiple errors for nested field in a dict:

        >>> validate = Schema({
        ...     'adict': {
        ...         'strfield': str,
        ...         'intfield': int
        ...     }
        ... })
        >>> try:
        ...     validate({
        ...         'adict': {
        ...             'strfield': 123,
        ...             'intfield': 'one'
        ...         }
        ...     })
        ... except er.MultipleInvalid as e:
        ...     print(sorted(str(i) for i in e.errors)) # doctest: +NORMALIZE_WHITESPACE
        ["expected int for dictionary value @ data['adict']['intfield']",
         "expected str for dictionary value @ data['adict']['strfield']"]

        """
        pass

    def _compile_sequence(self, schema, seq_type):
        """Validate a sequence type.

        This is a sequence of valid values or validators tried in order.

        >>> validator = Schema(['one', 'two', int])
        >>> validator(['one'])
        ['one']
        >>> with raises(er.MultipleInvalid, 'expected int @ data[0]'):
        ...   validator([3.5])
        >>> validator([1])
        [1]
        """
        pass

    def _compile_tuple(self, schema):
        """Validate a tuple.

        A tuple is a sequence of valid values or validators tried in order.

        >>> validator = Schema(('one', 'two', int))
        >>> validator(('one',))
        ('one',)
        >>> with raises(er.MultipleInvalid, 'expected int @ data[0]'):
        ...   validator((3.5,))
        >>> validator((1,))
        (1,)
        """
        pass

    def _compile_list(self, schema):
        """Validate a list.

        A list is a sequence of valid values or validators tried in order.

        >>> validator = Schema(['one', 'two', int])
        >>> validator(['one'])
        ['one']
        >>> with raises(er.MultipleInvalid, 'expected int @ data[0]'):
        ...   validator([3.5])
        >>> validator([1])
        [1]
        """
        pass

    def _compile_set(self, schema):
        """Validate a set.

        A set is an unordered collection of unique elements.

        >>> validator = Schema({int})
        >>> validator(set([42])) == set([42])
        True
        >>> with raises(er.Invalid, 'expected a set'):
        ...   validator(42)
        >>> with raises(er.MultipleInvalid, 'invalid value in set'):
        ...   validator(set(['a']))
        """
        pass

    def extend(self, schema: Schemable, required: typing.Optional[bool]=None, extra: typing.Optional[int]=None) -> Schema:
        """Create a new `Schema` by merging this and the provided `schema`.

        Neither this `Schema` nor the provided `schema` are modified. The
        resulting `Schema` inherits the `required` and `extra` parameters of
        this, unless overridden.

        Both schemas must be dictionary-based.

        :param schema: dictionary to extend this `Schema` with
        :param required: if set, overrides `required` of this `Schema`
        :param extra: if set, overrides `extra` of this `Schema`
        """
        pass

def _compile_scalar(schema):
    """A scalar value.

    The schema can either be a value or a type.

    >>> _compile_scalar(int)([], 1)
    1
    >>> with raises(er.Invalid, 'expected float'):
    ...   _compile_scalar(float)([], '1')

    Callables have
    >>> _compile_scalar(lambda v: float(v))([], '1')
    1.0

    As a convenience, ValueError's are trapped:

    >>> with raises(er.Invalid, 'not a valid value'):
    ...   _compile_scalar(lambda v: float(v))([], 'a')
    """
    pass

def _compile_itemsort():
    """return sort function of mappings"""
    pass
_sort_item = _compile_itemsort()

def _iterate_mapping_candidates(schema):
    """Iterate over schema in a meaningful order."""
    pass

def _iterate_object(obj):
    """Return iterator over object attributes. Respect objects with
    defined __slots__.

    """
    pass

class Msg(object):
    """Report a user-friendly message if a schema fails to validate.

    >>> validate = Schema(
    ...   Msg(['one', 'two', int],
    ...       'should be one of "one", "two" or an integer'))
    >>> with raises(er.MultipleInvalid, 'should be one of "one", "two" or an integer'):
    ...   validate(['three'])

    Messages are only applied to invalid direct descendants of the schema:

    >>> validate = Schema(Msg([['one', 'two', int]], 'not okay!'))
    >>> with raises(er.MultipleInvalid, 'expected int @ data[0][0]'):
    ...   validate([['three']])

    The type which is thrown can be overridden but needs to be a subclass of Invalid

    >>> with raises(er.SchemaError, 'Msg can only use subclases of Invalid as custom class'):
    ...   validate = Schema(Msg([int], 'should be int', cls=KeyError))

    If you do use a subclass of Invalid, that error will be thrown (wrapped in a MultipleInvalid)

    >>> validate = Schema(Msg([['one', 'two', int]], 'not okay!', cls=er.RangeInvalid))
    >>> try:
    ...  validate(['three'])
    ... except er.MultipleInvalid as e:
    ...   assert isinstance(e.errors[0], er.RangeInvalid)
    """

    def __init__(self, schema: Schemable, msg: str, cls: typing.Optional[typing.Type[Error]]=None) -> None:
        if cls and (not issubclass(cls, er.Invalid)):
            raise er.SchemaError('Msg can only use subclases of Invalid as custom class')
        self._schema = schema
        self.schema = Schema(schema)
        self.msg = msg
        self.cls = cls

    def __call__(self, v):
        try:
            return self.schema(v)
        except er.Invalid as e:
            if len(e.path) > 1:
                raise e
            else:
                raise (self.cls or er.Invalid)(self.msg)

    def __repr__(self):
        return 'Msg(%s, %s, cls=%s)' % (self._schema, self.msg, self.cls)

class Object(dict):
    """Indicate that we should work with attributes, not keys."""

    def __init__(self, schema: typing.Any, cls: object=UNDEFINED) -> None:
        self.cls = cls
        super(Object, self).__init__(schema)

class VirtualPathComponent(str):

    def __str__(self):
        return '<' + self + '>'

    def __repr__(self):
        return self.__str__()

class Marker(object):
    """Mark nodes for special treatment.

    `description` is an optional field, unused by Voluptuous itself, but can be
    introspected by any external tool, for example to generate schema documentation.
    """
    __slots__ = ('schema', '_schema', 'msg', 'description', '__hash__')

    def __init__(self, schema_: Schemable, msg: typing.Optional[str]=None, description: typing.Any | None=None) -> None:
        self.schema: typing.Any = schema_
        self._schema = Schema(schema_)
        self.msg = msg
        self.description = description
        self.__hash__ = cache(lambda: hash(schema_))

    def __call__(self, v):
        try:
            return self._schema(v)
        except er.Invalid as e:
            if not self.msg or len(e.path) > 1:
                raise
            raise er.Invalid(self.msg)

    def __str__(self):
        return str(self.schema)

    def __repr__(self):
        return repr(self.schema)

    def __lt__(self, other):
        if isinstance(other, Marker):
            return self.schema < other.schema
        return self.schema < other

    def __eq__(self, other):
        return self.schema == other

    def __ne__(self, other):
        return not self.schema == other

class Optional(Marker):
    """Mark a node in the schema as optional, and optionally provide a default

    >>> schema = Schema({Optional('key'): str})
    >>> schema({})
    {}
    >>> schema = Schema({Optional('key', default='value'): str})
    >>> schema({})
    {'key': 'value'}
    >>> schema = Schema({Optional('key', default=list): list})
    >>> schema({})
    {'key': []}

    If 'required' flag is set for an entire schema, optional keys aren't required

    >>> schema = Schema({
    ...    Optional('key'): str,
    ...    'key2': str
    ... }, required=True)
    >>> schema({'key2':'value'})
    {'key2': 'value'}
    """

    def __init__(self, schema: Schemable, msg: typing.Optional[str]=None, default: typing.Any=UNDEFINED, description: typing.Any | None=None) -> None:
        super(Optional, self).__init__(schema, msg=msg, description=description)
        self.default = default_factory(default)

class Exclusive(Optional):
    """Mark a node in the schema as exclusive.

    Exclusive keys inherited from Optional:

    >>> schema = Schema({Exclusive('alpha', 'angles'): int, Exclusive('beta', 'angles'): int})
    >>> schema({'alpha': 30})
    {'alpha': 30}

    Keys inside a same group of exclusion cannot be together, it only makes sense for dictionaries:

    >>> with raises(er.MultipleInvalid, "two or more values in the same group of exclusion 'angles' @ data[<angles>]"):
    ...   schema({'alpha': 30, 'beta': 45})

    For example, API can provides multiple types of authentication, but only one works in the same time:

    >>> msg = 'Please, use only one type of authentication at the same time.'
    >>> schema = Schema({
    ... Exclusive('classic', 'auth', msg=msg):{
    ...     Required('email'): str,
    ...     Required('password'): str
    ...     },
    ... Exclusive('internal', 'auth', msg=msg):{
    ...     Required('secret_key'): str
    ...     },
    ... Exclusive('social', 'auth', msg=msg):{
    ...     Required('social_network'): str,
    ...     Required('token'): str
    ...     }
    ... })

    >>> with raises(er.MultipleInvalid, "Please, use only one type of authentication at the same time. @ data[<auth>]"):
    ...     schema({'classic': {'email': 'foo@example.com', 'password': 'bar'},
    ...             'social': {'social_network': 'barfoo', 'token': 'tEMp'}})
    """

    def __init__(self, schema: Schemable, group_of_exclusion: str, msg: typing.Optional[str]=None, description: typing.Any | None=None) -> None:
        super(Exclusive, self).__init__(schema, msg=msg, description=description)
        self.group_of_exclusion = group_of_exclusion

class Inclusive(Optional):
    """Mark a node in the schema as inclusive.

    Inclusive keys inherited from Optional:

    >>> schema = Schema({
    ...     Inclusive('filename', 'file'): str,
    ...     Inclusive('mimetype', 'file'): str
    ... })
    >>> data = {'filename': 'dog.jpg', 'mimetype': 'image/jpeg'}
    >>> data == schema(data)
    True

    Keys inside a same group of inclusive must exist together, it only makes sense for dictionaries:

    >>> with raises(er.MultipleInvalid, "some but not all values in the same group of inclusion 'file' @ data[<file>]"):
    ...     schema({'filename': 'dog.jpg'})

    If none of the keys in the group are present, it is accepted:

    >>> schema({})
    {}

    For example, API can return 'height' and 'width' together, but not separately.

    >>> msg = "Height and width must exist together"
    >>> schema = Schema({
    ...     Inclusive('height', 'size', msg=msg): int,
    ...     Inclusive('width', 'size', msg=msg): int
    ... })

    >>> with raises(er.MultipleInvalid, msg + " @ data[<size>]"):
    ...     schema({'height': 100})

    >>> with raises(er.MultipleInvalid, msg + " @ data[<size>]"):
    ...     schema({'width': 100})

    >>> data = {'height': 100, 'width': 100}
    >>> data == schema(data)
    True
    """

    def __init__(self, schema: Schemable, group_of_inclusion: str, msg: typing.Optional[str]=None, description: typing.Any | None=None, default: typing.Any=UNDEFINED) -> None:
        super(Inclusive, self).__init__(schema, msg=msg, default=default, description=description)
        self.group_of_inclusion = group_of_inclusion

class Required(Marker):
    """Mark a node in the schema as being required, and optionally provide a default value.

    >>> schema = Schema({Required('key'): str})
    >>> with raises(er.MultipleInvalid, "required key not provided @ data['key']"):
    ...   schema({})

    >>> schema = Schema({Required('key', default='value'): str})
    >>> schema({})
    {'key': 'value'}
    >>> schema = Schema({Required('key', default=list): list})
    >>> schema({})
    {'key': []}
    """

    def __init__(self, schema: Schemable, msg: typing.Optional[str]=None, default: typing.Any=UNDEFINED, description: typing.Any | None=None) -> None:
        super(Required, self).__init__(schema, msg=msg, description=description)
        self.default = default_factory(default)

class Remove(Marker):
    """Mark a node in the schema to be removed and excluded from the validated
    output. Keys that fail validation will not raise ``Invalid``. Instead, these
    keys will be treated as extras.

    >>> schema = Schema({str: int, Remove(int): str})
    >>> with raises(er.MultipleInvalid, "extra keys not allowed @ data[1]"):
    ...    schema({'keep': 1, 1: 1.0})
    >>> schema({1: 'red', 'red': 1, 2: 'green'})
    {'red': 1}
    >>> schema = Schema([int, Remove(float), Extra])
    >>> schema([1, 2, 3, 4.0, 5, 6.0, '7'])
    [1, 2, 3, 5, '7']
    """

    def __init__(self, schema_: Schemable, msg: typing.Optional[str]=None, description: typing.Any | None=None) -> None:
        super().__init__(schema_, msg, description)
        self.__hash__ = cache(lambda: object.__hash__(self))

    def __call__(self, schema: Schemable):
        super(Remove, self).__call__(schema)
        return self.__class__

    def __repr__(self):
        return 'Remove(%r)' % (self.schema,)

def message(default: typing.Optional[str]=None, cls: typing.Optional[typing.Type[Error]]=None) -> typing.Callable:
    """Convenience decorator to allow functions to provide a message.

    Set a default message:

        >>> @message('not an integer')
        ... def isint(v):
        ...   return int(v)

        >>> validate = Schema(isint())
        >>> with raises(er.MultipleInvalid, 'not an integer'):
        ...   validate('a')

    The message can be overridden on a per validator basis:

        >>> validate = Schema(isint('bad'))
        >>> with raises(er.MultipleInvalid, 'bad'):
        ...   validate('a')

    The class thrown too:

        >>> class IntegerInvalid(er.Invalid): pass
        >>> validate = Schema(isint('bad', clsoverride=IntegerInvalid))
        >>> try:
        ...  validate('a')
        ... except er.MultipleInvalid as e:
        ...   assert isinstance(e.errors[0], IntegerInvalid)
    """
    pass

def _args_to_dict(func, args):
    """Returns argument names as values as key-value pairs."""
    pass

def _merge_args_with_kwargs(args_dict, kwargs_dict):
    """Merge args with kwargs."""
    pass

def validate(*a, **kw) -> typing.Callable:
    """Decorator for validating arguments of a function against a given schema.

    Set restrictions for arguments:

        >>> @validate(arg1=int, arg2=int)
        ... def foo(arg1, arg2):
        ...   return arg1 * arg2

    Set restriction for returned value:

        >>> @validate(arg=int, __return__=int)
        ... def bar(arg1):
        ...   return arg1 * 2

    """
    pass
