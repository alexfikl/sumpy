from __future__ import division
from __future__ import absolute_import

__copyright__ = "Copyright (C) 2012 Andreas Kloeckner"

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

import numpy as np
import sympy as sp
from pytools import memoize_method

__doc__ = """
.. autoclass:: ExpansionBase
"""


# {{{ base class

class ExpansionBase(object):

    def __init__(self, kernel, order):
        # Don't be tempted to remove target derivatives here.
        # Line Taylor QBX can't do without them, because it can't
        # apply those derivatives to the expanded quantity.

        self.kernel = kernel
        self.order = order

    # {{{ propagate kernel interface

    @property
    def dim(self):
        return self.kernel.dim

    @property
    def is_complex_valued(self):
        return self.kernel.is_complex_valued

    def prepare_loopy_kernel(self, loopy_knl):
        return self.kernel.prepare_loopy_kernel(loopy_knl)

    def transform_to_code(self, expr):
        return self.kernel.transform_to_code(expr)

    def get_scaling(self):
        return self.kernel.get_scaling()

    def get_args(self):
        return self.kernel.get_args()

    def get_source_args(self):
        return self.kernel.get_source_args()

    # }}}

    def __len__(self):
        return len(self.get_coefficient_identifiers())

    def coefficients_from_source(self, avec, bvec):
        """Form an expansion from a source point.

        :arg avec: vector from source to center.
        :arg bvec: vector from center to target. Not usually necessary,
            except for line-Taylor expansion.

        :returns: a list of :mod:`sympy` expressions representing
            the coefficients of the expansion.
        """
        raise NotImplementedError

    def evaluate(self, coeffs, bvec):
        """
        :return: a :mod:`sympy` expression corresponding
            to the evaluated expansion with the coefficients
            in *coeffs*.
        """

        raise NotImplementedError

    def update_persistent_hash(self, key_hash, key_builder):
        key_hash.update(type(self).__name__.encode("utf8"))
        key_builder.rec(key_hash, self.kernel)
        key_builder.rec(key_hash, self.order)

    def __eq__(self, other):
        return (
                type(self) == type(other)
                and self.kernel == other.kernel
                and self.order == other.order)

    def __ne__(self, other):
        return not self.__eq__(other)

# }}}


# {{{ volume taylor

class VolumeTaylorExpansionBase(object):

    def get_coefficient_identifiers(self):
        """
        Returns the identifiers of the coefficients that actually get stored.
        """
        raise NotImplementedError

    @memoize_method
    def _storage_loc_dict(self):
        return dict((i, idx) for idx, i in
                    enumerate(self.get_coefficient_identifiers()))

    def get_storage_index(self, i):
        return self._storage_loc_dict[i]

    @memoize_method
    def get_full_coefficient_identifiers(self):
        """
        Returns identifiers for every coefficient in the complete expansion.
        """
        from pytools import (
                generate_nonnegative_integer_tuples_summing_to_at_most
                as gnitstam)

        return sorted(gnitstam(self.order, self.kernel.dim), key=sum)

    def stored_to_full(self, coeff_idx, stored_coeffs):
        raise NotImplementedError

    def full_to_stored(self, coeff_idx, full_coeffs):
        raise NotImplementedError


class VolumeTaylorExpansion(VolumeTaylorExpansionBase):

    get_coefficient_identifiers = (
        VolumeTaylorExpansionBase.get_full_coefficient_identifiers)

    def stored_to_full(self, stored_coeffs):
        return stored_coeffs

    full_to_stored = stored_to_full


class LinearRecurrenceBasedVolumeTaylorExpansion(VolumeTaylorExpansionBase):

    def __init__(self):
        self.precompute_coeff_matrix()

    def get_coefficient_identifiers(self):
        return self.stored_identifiers

    def stored_to_full(self, stored_coeffs):
        return self.coeff_matrix.dot(stored_coeffs)

    def full_to_stored(self, full_coeffs):
        return self.coeff_matrix.T.dot(full_coeffs)

    def precompute_coeff_matrix(self):
        stored_identifiers = []
        identifiers_so_far = []

        ncoeffs = len(self.get_full_coefficient_identifiers())
        coeff_matrix = []

        # Build up the matrix by row.
        for identifier in self.get_full_coefficient_identifiers():
            expr = self.try_get_recurrence_for_derivative(
                    identifier, identifiers_so_far, ncoeffs)

            if expr is None:
                # Identifier should be stored
                row = np.zeros(ncoeffs, dtype=object)
                row[len(stored_identifiers)] = 1
                stored_identifiers.append(identifier)
            else:
                # Rewrite in terms of the stored identifiers
                ncoeffs_so_far = len(coeff_matrix)
                row = np.dot(np.transpose(coeff_matrix), expr[:ncoeffs_so_far])

            coeff_matrix.append(row)
            identifiers_so_far.append(identifier)

        self.stored_identifiers = stored_identifiers
        ncols = len(stored_identifiers)
        self.coeff_matrix = np.vstack(coeff_matrix)[:, :ncols]


class LaplaceConformingVolumeTaylorExpansion(
        LinearRecurrenceBasedVolumeTaylorExpansion):

    def try_get_recurrence_for_derivative(self, deriv, in_terms_of, ncoeffs):
        deriv = np.array(deriv)

        for dim in np.nonzero(2 <= deriv)[0]:
            # Check if we can reduce this dimension in terms of the other
            # dimensions.

            reduced_deriv = deriv.copy()
            reduced_deriv[dim] -= 2

            needed_derivs = []
            for other_dim in range(self.kernel.dim):
                if other_dim == dim:
                    continue
                needed_deriv = reduced_deriv.copy()
                needed_deriv[other_dim] += 2

                needed_derivs.append(tuple(needed_deriv))

            expr = np.zeros(ncoeffs, dtype=object)
            try:
                for needed_deriv in needed_derivs:
                    deriv_idx = in_terms_of.index(needed_deriv)
                    expr[deriv_idx] = -1
            except ValueError:
                continue

            return expr


class HelmholtzConformingVolumeTaylorExpansion(
        LinearRecurrenceBasedVolumeTaylorExpansion):

    def try_get_recurrence_for_derivative(self, deriv, in_terms_of, ncoeffs):
        deriv = np.array(deriv)

        for dim in np.nonzero(2 <= deriv)[0]:
            # Check if we can reduce this dimension in terms of the other
            # dimensions.

            reduced_deriv = deriv.copy()
            reduced_deriv[dim] -= 2

            needed_derivs = []
            for other_dim in range(self.kernel.dim):
                if other_dim == dim:
                    continue
                needed_deriv = reduced_deriv.copy()
                needed_deriv[other_dim] += 2

                needed_derivs.append((-1, tuple(needed_deriv)))

            k = sp.Symbol(self.kernel.get_base_kernel().helmholtz_k_name)

            needed_derivs.append((-k*k, tuple(reduced_deriv)))

            expr = np.zeros(ncoeffs, dtype=object)
            try:
                for coeff, needed_deriv in needed_derivs:
                    deriv_idx = in_terms_of.index(needed_deriv)
                    expr[deriv_idx] = coeff
            except ValueError:
                continue

            return expr

# }}}


# vim: fdm=marker
