# -*- coding: utf-8 -*-

import collections
import itertools

from six import iteritems

from .common import NATIVE, PRIMITIVE, EMPTY_LIST, EMPTY_DICT
from .datastructures import OrderedDict, get_context_factory, DataObject
from .exceptions import ConversionError, ModelConversionError, ValidationError
from .types.compound import ModelType

try:
    basestring #PY2
except NameError:
    basestring = str #PY3

def _listify(value):
    if value is None:
        return []
    if isinstance(value, basestring):
        return [value]
    return list(value)

try:
    unicode #PY2
except:
    import codecs
    unicode = str #PY3


###
# Transform Loops
###

ImportContext = get_context_factory('ImportContext',
                    'field_converter, partial, strict, mapping, app_data')

ExportContext = get_context_factory('ExportContext',
                    'field_converter, role, raise_error_on_role, print_none, app_data')


def import_loop(cls, instance_or_dict, field_converter=None, trusted_data=None,
                partial=False, strict=False, mapping=None, app_data=None, context=None):
    """
    The import loop is designed to take untrusted data and convert it into the
    native types, as described in ``cls``.  It does this by calling
    ``field_converter`` on every field.

    Errors are aggregated and returned by throwing a ``ModelConversionError``.

    :param cls:
        The class for the model.
    :param instance_or_dict:
        A dict of data to be converted into types according to ``cls``.
    :param field_converter:
        This function is applied to every field found in ``instance_or_dict``.
    :param trusted_data:
        A ``dict``-like structure that may contain already validated data.
    :param partial:
        Allow partial data to validate; useful for PATCH requests.
        Essentially drops the ``required=True`` arguments from field
        definitions. Default: False
    :param strict:
        Complain about unrecognized keys. Default: False
    :param app_data:
        An arbitrary container for application-specific data that needs to
        be available during the conversion.
    :param context:
        An ``ImportContext`` object that encapsulates configuration options and
        ``app_data``. The context object is created upon the initial invocation
        of ``import_loop`` and is then propagated through the entire process.
    """
    if not isinstance(instance_or_dict, (cls, dict)):
        raise ModelConversionError('Model conversion requires a model or dict')

    mapping = mapping or {}
    app_data = app_data if app_data is not None else {}
    context = context or ImportContext(field_converter, partial, strict, mapping, app_data)

    data = dict(trusted_data) if trusted_data else {}
    errors = {}
    # Determine all acceptable field input names
    all_fields = set(cls._fields) ^ set(cls._serializables)
    for field_name, field, in iteritems(cls._fields):
        if field.serialized_name:
            all_fields.add(field.serialized_name)
        if field.deserialize_from:
            all_fields.update(set(_listify(field.deserialize_from)))
        if field_name in context.mapping:
            all_fields.update(set(_listify(context.mapping[field_name])))

    # Check for rogues if strict is set
    rogue_fields = set(instance_or_dict) - all_fields
    if context.strict and len(rogue_fields) > 0:
        for field in rogue_fields:
            errors[field] = 'Rogue field'

    for field_name, field in iteritems(cls._fields):
        trial_keys = _listify(field.deserialize_from)
        trial_keys.extend(_listify(context.mapping.get(field_name, [])))
        if field.serialized_name:
            serialized_field_name = field.serialized_name
            trial_keys.extend((serialized_field_name, field_name))
        else:
            serialized_field_name = field_name
            trial_keys.append(field_name)

        raw_value = None
        for key in trial_keys:
            if key and key in instance_or_dict:
                raw_value = instance_or_dict[key]
        if raw_value is None:
            if field_name in data:
                continue
            raw_value = field.default

        try:
            if raw_value is None:
                if field.required and not context.partial:
                    errors[serialized_field_name] = [field.messages['required']]
            else:
                field_params = {
                    'mapping': context.mapping.get('model_mapping', {}).get(field_name, None)
                }
                raw_value = field_converter(field, raw_value, context._branch(**field_params))

            data[field_name] = raw_value

        except ConversionError as exc:
            errors[serialized_field_name] = exc.messages
        except ValidationError as exc:
            errors[serialized_field_name] = exc.messages

    if errors:
        raise ModelConversionError(errors, data)

    return data


def export_loop(cls, instance_or_dict, field_converter=None, role=None,
                raise_error_on_role=True, print_none=False, app_data=None, context=None):
    """
    The export_loop function is intended to be a general loop definition that
    can be used for any form of data shaping, such as application of roles or
    how a field is transformed.

    :param cls:
        The model definition.
    :param instance_or_dict:
        The structure where fields from cls are mapped to values. The only
        expectionation for this structure is that it implements a ``dict``
        interface.
    :param field_converter:
        This function is applied to every field found in ``instance_or_dict``.
    :param role:
        The role used to determine if fields should be left out of the
        transformation.
    :param raise_error_on_role:
        This parameter enforces strict behavior which requires substructures
        to have the same role definition as their parent structures.
    :param print_none:
        This function overrides ``serialize_when_none`` values found either on
        ``cls`` or an instance.
    :param app_data:
        An arbitrary container for application-specific data that needs to
        be available during the conversion.
    :param context:
        An ``ExportContext`` object that encapsulates configuration options and
        ``app_data``. The context object is created upon the initial invocation
        of ``export_loop`` and is then propagated through the entire process.
    """
    app_data = app_data if app_data is not None else {}
    context = context or ExportContext(field_converter, role, raise_error_on_role, print_none, app_data)

    data = {}

    # Translate `role` into `gottago` function
    gottago = wholelist()
    if hasattr(cls, '_options') and context.role in cls._options.roles:
        gottago = cls._options.roles[context.role]
    elif context.role and context.raise_error_on_role:
        error_msg = u'%s Model has no role "%s"'
        raise ValueError(error_msg % (cls.__name__, context.role))
    else:
        gottago = cls._options.roles.get("default", gottago)

    fields_order = (getattr(cls._options, 'fields_order', None)
                    if hasattr(cls, '_options') else None)

    for field_name, field, value in atoms(cls, instance_or_dict):
        serialized_name = field.serialized_name or field_name

        # Skipping this field was requested
        if gottago(field_name, value):
            continue

        # Value found, apply transformation and store it
        elif value is not None:
            shaped = context.field_converter(field, value, context)
            feels_empty = shaped is None or field.is_compound and len(shaped) == 0

            # Print if we want none or found a value
            if feels_empty:
                if allow_none(cls, field) or context.print_none:
                    data[serialized_name] = shaped
            elif shaped is not None:
                data[serialized_name] = shaped

        # Store None if reqeusted
        elif allow_none(cls, field) or context.print_none:
            data[serialized_name] = value

    if fields_order:
        data = sort_dict(data, fields_order)

    return data


def sort_dict(dct, based_on):
    """
    Sorts provided dictionary based on order of keys provided in ``based_on``
    list.

    Order is not guarantied in case if ``dct`` has keys that are not present
    in ``based_on``

    :param dct:
        Dictionary to be sorted.
    :param based_on:
        List of keys in order that resulting dictionary should have.
    :return:
        OrderedDict with keys in the same order as provided ``based_on``.
    """
    return OrderedDict(
        sorted(
            dct.items(),
            key=lambda el: based_on.index(el[0] if el[0] in based_on else -1))
    )


def atoms(cls, instance_or_dict):
    """
    Iterator for the atomic components of a model definition and relevant data
    that creates a threeple of the field's name, the instance of it's type, and
    it's value.

    :param cls:
        The model definition.
    :param instance_or_dict:
        The structure where fields from cls are mapped to values. The only
        expectionation for this structure is that it implements a ``dict``
        interface.
    """
    all_fields = itertools.chain(iteritems(cls._fields),
                                 iteritems(cls._serializables))

    return ((field_name, field, instance_or_dict[field_name])
            for field_name, field in all_fields)


def allow_none(cls, field):
    """
    This function inspects a model and a field for a setting either at the
    model or field level for the ``serialize_when_none`` setting.

    The setting defaults to the value of the class.  A field can override the
    class setting with it's own ``serialize_when_none`` setting.

    :param cls:
        The model definition.
    :param field:
        The field in question.
    """
    allowed = cls._options.serialize_when_none
    if field.serialize_when_none is not None:
        allowed = field.serialize_when_none
    return allowed


###
# Field Filtering
###

class Role(collections.Set):

    """
    A ``Role`` object can be used to filter specific fields against a sequence.

    The ``Role`` is two things: a set of names and a function.  The function
    describes how filter taking a field name as input and then returning either
    ``True`` or ``False``, indicating that field should or should not be
    skipped.

    A ``Role`` can be operated on as a ``Set`` object representing the fields
    is has an opinion on.  When Roles are combined with other roles, the
    filtering behavior of the first role is used.
    """

    def __init__(self, function, fields):
        self.function = function
        self.fields = set(fields)

    def _from_iterable(self, iterable):
        return Role(self.function, iterable)

    def __contains__(self, value):
        return value in self.fields

    def __iter__(self):
        return iter(self.fields)

    def __len__(self):
        return len(self.fields)

    def __eq__(self, other):
        print(dir(self.function))
        return (self.function.__name__ == other.function.__name__ and
                self.fields == other.fields)

    def __str__(self):
        return '%s(%s)' % (self.function.__name__,
                           ', '.join("'%s'" % f for f in self.fields))

    def __repr__(self):
        return '<Role %s>' % str(self)

    # edit role fields
    def __add__(self, other):
        fields = self.fields.union(other)
        return self._from_iterable(fields)

    def __sub__(self, other):
        fields = self.fields.difference(other)
        return self._from_iterable(fields)

    # apply role to field
    def __call__(self, name, value):
        return self.function(name, value, self.fields)

    # static filter functions
    @staticmethod
    def wholelist(name, value, seq):
        """
        Accepts a field name, value, and a field list.  This functions
        implements acceptance of all fields by never requesting a field be
        skipped, thus returns False for all input.

        :param name:
            The field name to inspect.
        :param value:
            The field's value.
        :param seq:
            The list of fields associated with the ``Role``.
        """
        return False

    @staticmethod
    def whitelist(name, value, seq):
        """
        Implements the behavior of a whitelist by requesting a field be skipped
        whenever it's name is not in the list of fields.

        :param name:
            The field name to inspect.
        :param value:
            The field's value.
        :param seq:
            The list of fields associated with the ``Role``.
        """

        if seq is not None and len(seq) > 0:
            return name not in seq
        return True

    @staticmethod
    def blacklist(name, value, seq):
        """
        Implements the behavior of a blacklist by requesting a field be skipped
        whenever it's name is found in the list of fields.

        :param k:
            The field name to inspect.
        :param v:
            The field's value.
        :param seq:
            The list of fields associated with the ``Role``.
        """
        if seq is not None and len(seq) > 0:
            return name in seq
        return False


def wholelist(*field_list):
    """
    Returns a function that evicts nothing. Exists mainly to be an explicit
    allowance of all fields instead of a using an empty blacklist.
    """
    return Role(Role.wholelist, field_list)


def whitelist(*field_list):
    """
    Returns a function that operates as a whitelist for the provided list of
    fields.

    A whitelist is a list of fields explicitly named that are allowed.
    """
    return Role(Role.whitelist, field_list)


def blacklist(*field_list):
    """
    Returns a function that operates as a blacklist for the provided list of
    fields.

    A blacklist is a list of fields explicitly named that are not allowed.
    """
    return Role(Role.blacklist, field_list)


###
# Import and export functions
###


class FieldConverter(object):

    def __call__(self, field, value, context):
        raise NotImplementedError


class ExportConverter(FieldConverter):

    def __init__(self, format, exceptions=None):
        self.primary = format
        self.secondary = not format
        self.exceptions = set(exceptions) if exceptions else None

    def __call__(self, field, value, context):
        format = self.primary
        if self.exceptions:
            if any((issubclass(field.typeclass, cls) for cls in self.exceptions)):
                format = self.secondary
        return field.export(value, format, context)


_import_converter = lambda field, value, context: field.convert(value, context)

_to_native_converter = ExportConverter(NATIVE)
_to_dict_converter = ExportConverter(NATIVE, [ModelType])
_to_primitive_converter = ExportConverter(PRIMITIVE)


def convert(cls, instance_or_dict, **kwargs):
    return import_loop(cls, instance_or_dict, _import_converter, **kwargs)


def to_native(cls, instance_or_dict, **kwargs):
    return export_loop(cls, instance_or_dict, _to_native_converter, **kwargs)


def to_dict(cls, instance_or_dict, **kwargs):
    return export_loop(cls, instance_or_dict, _to_dict_converter, **kwargs)


def to_primitive(cls, instance_or_dict, **kwargs):
    return export_loop(cls, instance_or_dict, _to_primitive_converter, **kwargs)


def expand(data, expanded_data=None):
    """
    Expands a flattened structure into it's corresponding layers.  Essentially,
    it is the counterpart to ``flatten_to_dict``.

    :param data:
        The data to expand.
    :param expanded_data:
        Existing expanded data that this function use for output
    """
    expanded_dict = {}
    context = expanded_data or expanded_dict

    for key, value in iteritems(data):
        try:
            key, remaining = key.split(".", 1)
        except ValueError:
            if not (value in (EMPTY_DICT, EMPTY_LIST) and key in expanded_dict):
                expanded_dict[key] = value
        else:
            current_context = context.setdefault(key, {})
            if current_context in (EMPTY_DICT, EMPTY_LIST):
                current_context = {}
                context[key] = current_context

            current_context.update(expand({remaining: value}, current_context))
    return expanded_dict


def flatten_to_dict(instance_or_dict, prefix=None, ignore_none=True):
    """
    Flattens an iterable structure into a single layer dictionary.

    For example:

        {
            's': 'jms was hrrr',
            'l': ['jms was here', 'here', 'and here']
        }

        becomes

        {
            's': 'jms was hrrr',
            u'l.1': 'here',
            u'l.0': 'jms was here',
            u'l.2': 'and here'
        }

    :param instance_or_dict:
        The structure where fields from cls are mapped to values. The only
        expectionation for this structure is that it implements a ``dict``
        interface.
    :param ignore_none:
        This ignores any ``serialize_when_none`` settings and forces the empty
        fields to be printed as part of the flattening.
        Default: True
    :param prefix:
        This puts a prefix in front of the field names during flattening.
        Default: None
    """
    if isinstance(instance_or_dict, dict):
        iterator = iteritems(instance_or_dict)
    # if hasattr(instance_or_dict, "iteritems"):
    #     iterator = instance_or_dict.iteritems()
    else:
        iterator = enumerate(instance_or_dict)

    flat_dict = {}
    for key, value in iterator:
        if prefix:
            key = ".".join(map(unicode, (prefix, key)))

        if value == []:
            value = EMPTY_LIST
        elif value == {}:
            value = EMPTY_DICT

        if isinstance(value, (dict, list)):
            flat_dict.update(flatten_to_dict(value, prefix=key))
        elif value is not None:
            flat_dict[key] = value
        elif not ignore_none:
            flat_dict[key] = None

    return flat_dict


def flatten(cls, instance_or_dict, role=None, raise_error_on_role=True,
            ignore_none=True, prefix=None, app_data=None, context=None):
    """
    Produces a flat dictionary representation of the model.  Flat, in this
    context, means there is only one level to the dictionary.  Multiple layers
    are represented by the structure of the key.

    Example:

        >>> class Foo(Model):
        ...    s = StringType()
        ...    l = ListType(StringType)

        >>> f = Foo()
        >>> f.s = 'string'
        >>> f.l = ['jms', 'was here', 'and here']

        >>> flatten(Foo, f)
        {'s': 'string', u'l.1': 'jms', u'l.0': 'was here', u'l.2': 'and here'}

    :param cls:
        The model definition.
    :param instance_or_dict:
        The structure where fields from cls are mapped to values. The only
        expectionation for this structure is that it implements a ``dict``
        interface.
    :param role:
        The role used to determine if fields should be left out of the
        transformation.
    :param raise_error_on_role:
        This parameter enforces strict behavior which requires substructures
        to have the same role definition as their parent structures.
    :param ignore_none:
        This ignores any ``serialize_when_none`` settings and forces the empty
        fields to be printed as part of the flattening.
        Default: True
    :param prefix:
        This puts a prefix in front of the field names during flattening.
        Default: None
    """
    data = to_primitive(cls, instance_or_dict, role=role, raise_error_on_role=raise_error_on_role,
                        print_none=True, app_data=app_data, context=context)

    flattened = flatten_to_dict(data, prefix=prefix, ignore_none=ignore_none)

    return flattened

