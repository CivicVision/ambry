"""

One of three degrees of enforcement may be specified by passing
the 'debug' keyword argument to the decorator:
    0 -- NONE:   No type-checking. Decorators disabled.
 #!python
-- MEDIUM: Print warning message to stderr. (Default)
    2 -- STRONG: Raise TypeError with message.
If 'debug' is not passed to the decorator, the default level is used.

Example usage:
    >>> NONE, MEDIUM, STRONG = 0, 1, 2
    >>>
    >>> @accepts(int, int, int)
    ... @returns(float)
    ... def average(x, y, z):
    ...     return (x + y + z) / 2
    ...
    >>> average(5.5, 10, 15.0)
    TypeWarning:  'average' method accepts (int, int, int), but was given
    (float, int, float)
    15.25
    >>> average(5, 10, 15)
    TypeWarning:  'average' method returns (float), but result is (int)
    15

Needed to cast params as floats in function def (or simply divide by 2.0).

    >>> TYPE_CHECK = STRONG
    >>> @accepts(int, debug=TYPE_CHECK)
    ... @returns(int, debug=TYPE_CHECK)
    ... def fib(n):
    ...     if n in (0, 1): return n
    ...     return fib(n-1) + fib(n-2)
    ...
    >>> fib(5.3)
    Traceback (most recent call last):
      ...
    TypeError: 'fib' method accepts (int), but was given (float)
"""
import sys


def accepts(*types, **kw):
    """Function decorator. Checks decorated function's arguments are of the
    expected types.

    Parameters:
    types -- The expected types of the inputs to the decorated function.
             Must specify type for each parameter.
    kw    -- Optional specification of 'debug' level (this is the only valid
             keyword argument, no other should be given).
             debug = ( 0 | 1 | 2 )

    """
    if not kw:
        # default level: MEDIUM
        debug = 1
    else:
        debug = kw['debug']
    try:
        def decorator(f):
            def newf(*args):
                if debug is 0:
                    return f(*args)
                assert len(args) == len(types), "Expected {} args, got {}".format(len(types), len(args))
                argtypes = tuple(map(type, args))
                if argtypes != types:
                    msg = info(f.__name__, types, argtypes, 0)
                    if debug is 1:
                        print >> sys.stderr, 'TypeWarning: ', msg
                    elif debug is 2:
                        raise TypeError(msg)
                return f(*args)
            newf.__name__ = f.__name__
            return newf
        return decorator
    except KeyError as key:
        raise KeyError(key + "is not a valid keyword argument")
    except TypeError as msg:
        raise TypeError(msg)


def returns(ret_type, **kw):
    """Function decorator. Checks decorated function's return value is of the
    expected type.

    Parameters:
    ret_type -- The expected type of the decorated function's return value.
                Must specify type for each parameter.
    kw       -- Optional specification of 'debug' level (this is the only valid
                keyword argument, no other should be given).
                debug=(0 | 1 | 2)

    """
    try:
        if not kw:
            # default level: MEDIUM
            debug = 1
        else:
            debug = kw['debug']

        def decorator(f):
            def newf(*args):
                result = f(*args)
                if debug is 0:
                    return result
                res_type = type(result)
                if res_type != ret_type:
                    msg = info(f.__name__, (ret_type,), (res_type,), 1)
                    if debug is 1:
                        print >> sys.stderr, 'TypeWarning: ', msg
                    elif debug is 2:
                        raise TypeError(msg)
                return result
            newf.__name__ = f.__name__
            return newf
        return decorator
    except KeyError as key:
        raise KeyError(key + "is not a valid keyword argument")
    except TypeError as msg:
        raise TypeError(msg)


def info(fname, expected, actual, flag):
    """Convenience function returns nicely formatted error/warning msg."""
    format = lambda types: ', '.join([str(t).split("'")[1] for t in types])
    expected, actual = format(expected), format(actual)
    msg = "'{}' method ".format(fname) \
          + ("accepts", "returns")[flag] + " ({}), but ".format(expected) \
          + ("was given", "result is")[flag] + " ({})".format(actual)
    return msg
