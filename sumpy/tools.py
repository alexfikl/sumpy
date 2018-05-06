from __future__ import division, absolute_import

__copyright__ = """
Copyright (C) 2012 Andreas Kloeckner
Copyright (C) 2018 Alexandru Fikl
"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import six
from six.moves import range, zip
from pytools import memoize_method, memoize_in
import numpy as np
import sumpy.symbolic as sym

import loopy as lp
from loopy.version import MOST_RECENT_LANGUAGE_VERSION

import logging
logger = logging.getLogger(__name__)


# {{{ multi_index helpers

def add_mi(mi1, mi2):
    return tuple(mi1i+mi2i for mi1i, mi2i in zip(mi1, mi2))


def mi_factorial(mi):
    from pytools import factorial
    result = 1
    for mi_i in mi:
        result *= factorial(mi_i)
    return result


def mi_power(vector, mi):
    result = 1
    for mi_i, vec_i in zip(mi, vector):
        result *= vec_i**mi_i
    return result


class MiDerivativeTaker(object):

    def __init__(self, expr, var_list):
        assert isinstance(expr, sym.Basic)
        self.var_list = var_list
        empty_mi = (0,) * len(var_list)
        self.cache_by_mi = {empty_mi: expr}

    def mi_dist(self, a, b):
        return np.array(a, dtype=int) - np.array(b, dtype=int)

    def diff(self, mi):
        try:
            expr = self.cache_by_mi[mi]
        except KeyError:
            current_mi = self.get_closest_cached_mi(mi)
            expr = self.cache_by_mi[current_mi]

            for next_deriv, next_mi in self.get_derivative_taking_sequence(
                    current_mi, mi):
                expr = expr.diff(next_deriv)
                self.cache_by_mi[next_mi] = expr

        return expr

    def get_derivative_taking_sequence(self, start_mi, end_mi):
        current_mi = np.array(start_mi, dtype=int)
        for idx, (mi_i, vec_i) in enumerate(
                zip(self.mi_dist(end_mi, start_mi), self.var_list)):
            for i in range(1, 1 + mi_i):
                current_mi[idx] += 1
                yield vec_i, tuple(current_mi)

    def get_closest_cached_mi(self, mi):
        return min((other_mi
                for other_mi in self.cache_by_mi.keys()
                if (np.array(mi) >= np.array(other_mi)).all()),
            key=lambda other_mi: sum(self.mi_dist(mi, other_mi)))


class LinearRecurrenceBasedMiDerivativeTaker(MiDerivativeTaker):
    """
    The derivative taker for expansions that use
    :class:`sumpy.expansion.LinearRecurrenceBasedDerivativeWrangler`
    """

    def __init__(self, expr, var_list, wrangler):
        super(LinearRecurrenceBasedMiDerivativeTaker, self).__init__(
                expr, var_list)
        self.wrangler = wrangler

    @memoize_method
    def diff(self, mi):
        """
        :arg mi: a multi-index (tuple) indicating how many x/y derivatives are
            to be taken.
        """
        try:
            expr = self.cache_by_mi[mi]
        except KeyError:
            from six import iteritems
            from sumpy.symbolic import Add

            closest_mi = self.get_closest_cached_mi(mi)
            expr = self.cache_by_mi[closest_mi]

            # Try to reduce the derivative using recurrences first, and if that
            # fails fall back to derivative taking.
            for next_deriv, next_mi in (
                        self.get_derivative_taking_sequence(closest_mi, mi)):

                recurrence = (
                        self.wrangler.try_get_recurrence_for_derivative(
                            next_mi, self.cache_by_mi))

                if recurrence is not None:
                    expr = Add(*tuple(
                            coeff * self.cache_by_mi[ident]
                            for ident, coeff in iteritems(recurrence)))
                else:
                    expr = expr.diff(next_deriv)

                self.cache_by_mi[next_mi] = expr

        return expr

# }}}


def build_matrix(op, dtype=None, shape=None):
    dtype = dtype or op.dtype
    from pytools import ProgressBar
    shape = shape or op.shape
    rows, cols = shape
    pb = ProgressBar("matrix", cols)
    mat = np.zeros(shape, dtype)

    try:
        matvec_method = op.matvec
    except AttributeError:
        matvec_method = op.__call__

    for i in range(cols):
        unit_vec = np.zeros(cols, dtype=dtype)
        unit_vec[i] = 1
        mat[:, i] = matvec_method(unit_vec)
        pb.progress()

    pb.finished()

    return mat


def vector_to_device(queue, vec):
    from pytools.obj_array import with_object_array_or_scalar

    from pyopencl.array import to_device

    def to_dev(ary):
        return to_device(queue, ary)

    return with_object_array_or_scalar(to_dev, vec)


def vector_from_device(queue, vec):
    from pytools.obj_array import with_object_array_or_scalar

    def from_dev(ary):
        from numbers import Number
        if isinstance(ary, (np.number, Number)):
            # zero, most likely
            return ary

        return ary.get(queue=queue)

    return with_object_array_or_scalar(from_dev, vec)


def gather_arguments(kernel_likes):
    result = {}
    for knl in kernel_likes:
        for arg in knl.get_args():
            result[arg.name] = arg
            # FIXME: possibly check that arguments match before overwriting

    return sorted(six.itervalues(result), key=lambda arg: arg.name)


def gather_source_arguments(kernel_likes):
    result = {}
    for knl in kernel_likes:
        for arg in knl.get_args() + knl.get_source_args():
            result[arg.name] = arg
            # FIXME: possibly check that arguments match before overwriting

    return sorted(six.itervalues(result), key=lambda arg: arg.name)


def gather_loopy_arguments(kernel_likes):
    return [arg.loopy_arg for arg in gather_arguments(kernel_likes)]


def gather_loopy_source_arguments(kernel_likes):
    return [arg.loopy_arg for arg in gather_source_arguments(kernel_likes)]


# {{{  KernelComputation

class KernelComputation(object):
    """Common input processing for kernel computations."""

    def __init__(self, ctx, kernels, strength_usage,
            value_dtypes, name, options=[], device=None):
        """
        :arg kernels: list of :class:`sumpy.kernel.Kernel` instances
            :class:`sumpy.kernel.TargetDerivative` wrappers should be
            the outermost kernel wrappers, if present.
        :arg strength_usage: A list of integers indicating which expression
            uses which density. This implicitly specifies the
            number of density arrays that need to be passed.
            Default: all kernels use the same density.
        """

        # {{{ process value_dtypes

        if value_dtypes is None:
            value_dtypes = []
            for knl in kernels:
                if knl.is_complex_valued:
                    value_dtypes.append(np.complex128)
                else:
                    value_dtypes.append(np.float64)

        if not isinstance(value_dtypes, (list, tuple)):
            value_dtypes = [np.dtype(value_dtypes)] * len(kernels)
        value_dtypes = [np.dtype(vd) for vd in value_dtypes]

        # }}}

        # {{{ process strength_usage

        if strength_usage is None:
            strength_usage = [0] * len(kernels)

        if len(kernels) != len(strength_usage):
            raise ValueError("exprs and strength_usage must have the same length")
        strength_count = max(strength_usage)+1

        # }}}

        if device is None:
            device = ctx.devices[0]

        self.context = ctx
        self.device = device

        self.kernels = kernels
        self.value_dtypes = value_dtypes
        self.strength_usage = strength_usage
        self.strength_count = strength_count

        self.name = name or self.default_name

    def get_kernel_scaling_assignments(self):
        from sumpy.symbolic import SympyToPymbolicMapper
        sympy_conv = SympyToPymbolicMapper()

        import loopy as lp
        return [
                lp.Assignment(id=None,
                    assignee="knl_%d_scaling" % i,
                    expression=sympy_conv(kernel.get_global_scaling_const()),
                    temp_var_type=dtype)
                for i, (kernel, dtype) in enumerate(
                    zip(self.kernels, self.value_dtypes))]

# }}}


# {{{

class MatrixBlockIndex(object):
    def __init__(self, queue, tgtindices, srcindices, tgtranges, srcranges):
        """Keep track of different ways to index into matrix blocks.

        In this description, a block is given by a subset of indices of
        the global matrix operator. For example, the :math:`i`-th block of
        a matrix `mat` using the input arguments is obtained by:

        .. code-block:: python

            mat[np.ix_(tgtindices[tgtranges[i]:tgtranges[i + 1]],
                       srcindices[srcranges[i]:srcranges[i + 1]])]

        :arg tgtindices: a subset of the full matrix row index set.
        :arg srcindices: a subset of the full matrix column index set.
        :arg tgtranges: an array used to index into `tgtindices`.
        :arg srcranges: an array used to index into `srcindices`.
        """

        self.queue = queue
        self.tgtindices = tgtindices
        self.tgtranges = tgtranges
        self.srcindices = srcindices
        self.srcranges = srcranges

    @memoize_method
    def linear_ranges(self):
        """
        :return: an array containing the cummulative sum of all block sizes.
            It can be used to index into a linear representation of the
            matrix blocks.
        """

        @memoize_in(self, "block_cumsum_knl")
        def cumsum():
            loopy_knl = lp.make_kernel(
                "{[i, j]: 0 <= i < nranges and 0 <= j <= i}",
                """
                blkranges[0] = 0
                blkranges[i + 1] = reduce(sum, j, \
                    (srcranges[j + 1] - srcranges[j]) * \
                    (tgtranges[j + 1] - tgtranges[j])) \
                """,
                [
                    lp.GlobalArg("tgtranges", None, shape="nranges + 1"),
                    lp.GlobalArg("srcranges", None, shape="nranges + 1"),
                    lp.GlobalArg("blkranges", np.int32, shape="nranges + 1"),
                    lp.ValueArg("nranges", None)
                ],
                name="block_cumsum_knl",
                default_offset=lp.auto,
                assumptions="nranges>=1",
                lang_version=MOST_RECENT_LANGUAGE_VERSION)

            loopy_knl = lp.realize_reduction(loopy_knl, force_scan=True,
                force_outer_iname_for_scan="i")
            return loopy_knl

        _, (blkranges,) = cumsum()(self.queue,
            tgtranges=self.tgtranges, srcranges=self.srcranges)

        return blkranges

    @memoize_method
    def linear_indices(self):
        """
        :return: a tuple of `(rowindices, colindices)` that can be used in
            conjunction with :func:`linear_ranges` to provide linear indexing
            into a set of matrix blocks. These index arrays are just the
            concatenated Cartesian products of all the block arrays described
            by :attr:`tgtindices` and :arg:`srcindices`.

            They can be used to index directly into the matrix as follows:

            .. code-block:: python

                mat[rowindices[blkranges[i]:blkranges[i + 1]],
                    colindices[blkranges[i]:blkranges[i + 1]]]
        """

        @memoize_in(self, "block_index_knl")
        def linear_index():
            loopy_knl = lp.make_kernel([
                "{[irange]: 0 <= irange < nranges}",
                "{[itgt, isrc]: 0 <= itgt < ntgtblock and 0 <= isrc < nsrcblock}"
                ],
                """
                for irange
                    <> ntgtblock = tgtranges[irange + 1] - tgtranges[irange]
                    <> nsrcblock = srcranges[irange + 1] - srcranges[irange]

                    for itgt, isrc
                        <> imat = blkranges[irange] + (nsrcblock * itgt + isrc)

                        rowindices[imat] = tgtindices[tgtranges[irange] + itgt] \
                            {id_prefix=write_index}
                        colindices[imat] = srcindices[srcranges[irange] + isrc] \
                            {id_prefix=write_index}
                    end
                end
                """,
                [
                    lp.GlobalArg('blkranges', None, shape="nranges + 1"),
                    lp.GlobalArg("rowindices", None, shape="nresults"),
                    lp.GlobalArg("colindices", None, shape="nresults"),
                    lp.ValueArg("nresults", None),
                    '...'
                ],
                name="block_index_knl",
                default_offset=lp.auto,
                assumptions='nranges>=1',
                silenced_warnings="write_race(write_index*)",
                lang_version=MOST_RECENT_LANGUAGE_VERSION)
            loopy_knl = lp.split_iname(loopy_knl, "irange", 128, outer_tag="g.0")

            return loopy_knl

        blkranges = self.linear_ranges()
        _, (rowindices, colindices) = linear_index()(self.queue,
            tgtindices=self.tgtindices, srcindices=self.srcindices,
            tgtranges=self.tgtranges, srcranges=self.srcranges,
            blkranges=blkranges, nresults=blkranges[-1])

        return rowindices, colindices

# }}}


# {{{ OrderedSet

# Source: http://code.activestate.com/recipes/576694-orderedset/
# Author: Raymond Hettinger
# License: MIT

import collections


class OrderedSet(collections.MutableSet):

    def __init__(self, iterable=None):
        self.end = end = []
        end += [None, end, end]         # sentinel node for doubly linked list
        self.map = {}                   # key --> [key, prev, next]
        if iterable is not None:
            self |= iterable

    def __len__(self):
        return len(self.map)

    def __contains__(self, key):
        return key in self.map

    def add(self, key):
        if key not in self.map:
            end = self.end
            curr = end[1]
            curr[2] = end[1] = self.map[key] = [key, curr, end]

    def discard(self, key):
        if key in self.map:
            key, prev, next = self.map.pop(key)
            prev[2] = next
            next[1] = prev

    def __iter__(self):
        end = self.end
        curr = end[2]
        while curr is not end:
            yield curr[0]
            curr = curr[2]

    def __reversed__(self):
        end = self.end
        curr = end[1]
        while curr is not end:
            yield curr[0]
            curr = curr[1]

    def pop(self, last=True):
        if not self:
            raise KeyError('set is empty')
        key = self.end[1][0] if last else self.end[2][0]
        self.discard(key)
        return key

    def __repr__(self):
        if not self:
            return '%s()' % (self.__class__.__name__,)
        return '%s(%r)' % (self.__class__.__name__, list(self))

    def __eq__(self, other):
        if isinstance(other, OrderedSet):
            return len(self) == len(other) and list(self) == list(other)
        return set(self) == set(other)

# }}}


class KernelCacheWrapper(object):
    @memoize_method
    def get_cached_optimized_kernel(self, **kwargs):
        from sumpy import code_cache, CACHING_ENABLED, OPT_ENABLED

        if CACHING_ENABLED:
            import loopy.version
            from sumpy.version import KERNEL_VERSION
            cache_key = (
                    self.get_cache_key()
                    + tuple(sorted(six.iteritems(kwargs)))
                    + (loopy.version.DATA_MODEL_VERSION,)
                    + (KERNEL_VERSION,)
                    + (OPT_ENABLED,))

            try:
                result = code_cache[cache_key]
                logger.debug("%s: kernel cache hit [key=%s]" % (
                    self.name, cache_key))
                return result
            except KeyError:
                pass

        logger.info("%s: kernel cache miss" % self.name)
        if CACHING_ENABLED:
            logger.info("%s: kernel cache miss [key=%s]" % (
                self.name, cache_key))

        from pytools import MinRecursionLimit
        with MinRecursionLimit(3000):
            if OPT_ENABLED:
                knl = self.get_optimized_kernel(**kwargs)
            else:
                knl = self.get_kernel()

        if CACHING_ENABLED:
            code_cache.store_if_not_present(cache_key, knl)

        return knl


def my_syntactic_subs(expr, subst_dict):
    # Workaround for differing substitution semantics between sympy and symengine.
    # FIXME: This is a hack.
    from sumpy.symbolic import Basic, Subs, Derivative

    if not isinstance(expr, Basic):
        return expr

    elif expr.is_Symbol:
        return subst_dict.get(expr, expr)

    elif isinstance(expr, Subs):
        new_point = tuple(my_syntactic_subs(p, subst_dict) for p in expr.point)

        import six
        new_subst_dict = dict(
            (var, subs) for var, subs in six.iteritems(subst_dict)
            if var not in expr.variables)

        new_expr = my_syntactic_subs(expr.expr, new_subst_dict)

        if new_point != expr.point or new_expr != expr.expr:
            return Subs(new_expr, expr.variables, new_point)

        return expr

    elif isinstance(expr, Derivative):
        new_expr = my_syntactic_subs(expr.expr, subst_dict)
        new_variables = my_syntactic_subs(expr.variables, subst_dict)

        if new_expr != expr.expr or any(new_var != var for new_var, var in
                                          zip(new_variables, expr.variables)):
            return Derivative(new_expr, *new_variables)

        return expr

    else:
        new_args = tuple(my_syntactic_subs(arg, subst_dict) for arg in expr.args)

        if any(new_arg != arg for arg, new_arg in zip(expr.args, new_args)):
            return expr.func(*new_args)

        return expr


# vim: fdm=marker
