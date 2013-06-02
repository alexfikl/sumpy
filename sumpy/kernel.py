from __future__ import division

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


import loopy as lp
import sympy as sp
import numpy as np
from pymbolic.primitives import Expression
from pymbolic.mapper import IdentityMapper


# {{{ basic kernel interface

class Kernel(object):
    """Basic kernel interface.

    .. attribute:: is_complex_valued
    .. attribute:: dimensions

        *dimensions* is allowed to be *None* if the dimensionality is not yet
        known. Use the :meth:`fix_dimensions`
    """

    def __init__(self, dimensions=None):
        self.dimensions = dimensions

    def fix_dimensions(self, dimensions):
        """Return a new :class:`Kernel` with :attr:`dimensions` set to
        *dimensions*.
        """

        raise NotImplementedError

    def get_base_kernel(self):
        return self

    def prepare_loopy_kernel(self, loopy_knl):
        """Apply some changes (such as registering function
        manglers) to the kernel. Return the new kernel.
        """
        return loopy_knl

    def transform_to_code(self, expr):
        """Postprocess the :mod:`pymbolic` expression
        generated from the result of :meth:`get_expression`
        on the way to code generation.
        """
        return expr

    def get_expression(self, dist_vec):
        """Return a :mod:`pymbolic` expression for the kernel.

        :arg dist_vec: target - source

        (Assumes translation invariance of the kernel.)
        """
        raise NotImplementedError

    def postprocess_at_source(self, expr, avec):
        """Transform a kernel evaluation or expansion expression in a place
        where the vector a (something-source) is known. ("something" may be
        an expansion center or a target.)
        """
        return expr

    def postprocess_at_target(self, expr, bvec):
        """Transform a kernel evaluation or expansion expression in a place
        where the vector b (target-something) is known. ("something" may be
        an expansion center or a target.)
        """
        return expr

    def get_scaling(self):
        """Return a global scaling of the kernel."""
        raise NotImplementedError

    def get_args(self):
        """Return list of :cls:`loopy.Argument` instances describing
        extra arguments used by kernel.
        """
        return []

    def get_preambles(self):
        return []

    def __sub__(self, other):
        return DifferenceKernel(self, other)

# }}}


# {{{ PDE kernels

class LaplaceKernel(Kernel):
    is_complex_valued = False

    def __repr__(self):
        if self.dimensions is not None:
            return "Laplace(%d)" % self.dimensions
        else:
            return "Laplace()"

    def fix_dimensions(self, dimensions):
        """Return a new :class:`Kernel` with :attr:`dimensions` set to
        *dimensions*.
        """
        return LaplaceKernel(dimensions)

    def get_expression(self, dist_vec):
        assert self.dimensions == len(dist_vec)
        from sumpy.symbolic import sympy_real_norm_2
        r = sympy_real_norm_2(dist_vec)

        if self.dimensions == 2:
            return sp.log(r)
        elif self.dimensions == 3:
            return 1/r
        else:
            raise RuntimeError("unsupported dimensionality")

    def get_scaling(self):
        """Return a global scaling of the kernel."""

        if self.dimensions == 2:
            return 1/(-2*sp.pi)
        elif self.dimensions == 3:
            return 1/(4*sp.pi)
        else:
            raise RuntimeError("unsupported dimensionality")


class HelmholtzKernel(Kernel):
    def __init__(self, dimensions=None, helmholtz_k_name="k",
            allow_evanescent=False):
        Kernel.__init__(self, dimensions)
        self.helmholtz_k_name = helmholtz_k_name
        self.allow_evanescent = allow_evanescent

    def __repr__(self):
        if self.dimensions is not None:
            return "Helmh(dim=%s, %s)" % (
                    self.dimensions, self.helmholtz_k_name)
        else:
            return "Helmh(%s)" % (self.helmholtz_k_name)

    def fix_dimensions(self, dimensions):
        """Return a new :class:`Kernel` with :attr:`dimensions` set to
        *dimensions*.
        """
        return HelmholtzKernel(dimensions, self.helmholtz_k_name,
                self.allow_evanescent)

    is_complex_valued = True

    def prepare_loopy_kernel(self, loopy_knl):
        # does loopy_knl already know about hank1_01?
        mangle_result = loopy_knl.mangle_function(
                "hank1_01", (np.dtype(np.complex128),))
        from sumpy.codegen import hank1_01_result_dtype, bessel_mangler
        if mangle_result is not hank1_01_result_dtype:
            return loopy_knl.register_function_mangler(bessel_mangler)
        else:
            return loopy_knl

    def get_expression(self, dist_vec):
        assert self.dimensions == len(dist_vec)

        from sumpy.symbolic import sympy_real_norm_2
        r = sympy_real_norm_2(dist_vec)

        k = sp.Symbol(self.helmholtz_k_name)

        if self.dimensions == 2:
            return sp.Function("hankel_1")(0, k*r)
        elif self.dimensions == 3:
            return sp.exp(sp.I*k*r)/r
        else:
            raise RuntimeError("unsupported dimensionality")

    def get_scaling(self):
        """Return a global scaling of the kernel."""

        if self.dimensions == 2:
            return sp.I/4
        elif self.dimensions == 3:
            return 1/(4*sp.pi)
        else:
            raise RuntimeError("unsupported dimensionality")

    def get_args(self):
        if self.allow_evanescent:
            k_dtype = np.complex128
        else:
            k_dtype = np.float64

        return [lp.ValueArg(self.helmholtz_k_name, k_dtype)]

    def get_preambles(self):
        from sumpy.codegen import BESSEL_PREAMBLE
        return [("sumpy-bessel", BESSEL_PREAMBLE)]


class DifferenceKernel(Kernel):
    def __init__(self, kernel_plus, kernel_minus):
        if (kernel_plus.get_base_kernel() is not kernel_plus
                or kernel_minus.get_base_kernel() is not kernel_minus):
            raise ValueError("kernels in difference kernel "
                    "must be basic, unwrapped PDE kernels")

        if kernel_plus.dimensions != kernel_minus.dimensions:
            raise ValueError(
                    "kernels in difference kernel have different dimensions")

        self.kernel_plus = kernel_plus
        self.kernel_minus = kernel_minus

        Kernel.__init__(self, self.kernel_plus.dimensions)

    def fix_dimensions(self, dimensions):
        """Return a new :class:`Kernel` with :attr:`dimensions` set to
        *dimensions*.
        """
        return DifferenceKernel(
                self.kernel_plus.fix_dimensions(dimensions),
                self.kernel_minus.fix_dimensions(dimensions))

    # FIXME mostly unimplemented

# }}}


def normalize_kernel(kernel):
    if not isinstance(kernel, Kernel):
        if kernel == 0:
            kernel = LaplaceKernel()
        elif isinstance(kernel, str):
            kernel = HelmholtzKernel(None, kernel)
        else:
            raise ValueError("Only Kernel instances, 0 (for Laplace) and "
                    "variable names (strings) "
                    "for the Helmholtz parameter are allowed as kernels.")

    return kernel


def count_derivatives(kernel):
    if isinstance(kernel, DerivativeBase):
        return 1 + count_derivatives(kernel.kernel)
    elif isinstance(kernel, KernelWrapper):
        return count_derivatives(kernel.kernel)
    else:
        return 0


def remove_axis_target_derivatives(kernel):
    if isinstance(kernel, AxisTargetDerivative):
        return remove_axis_target_derivatives(kernel.kernel)
    elif isinstance(kernel, KernelWrapper):
        kernel.replace_inner_kernel(
                remove_axis_target_derivatives(kernel.kernel))
    else:
        return kernel


# {{{ a kernel defined as wrapping another one--e.g., derivatives

class KernelWrapper(Kernel):
    def __init__(self, kernel):
        Kernel.__init__(self, kernel.dimensions)
        self.kernel = kernel

    def get_base_kernel(self):
        return self.kernel.get_base_kernel()

    def prepare_loopy_kernel(self, loopy_knl):
        return self.kernel.prepare_loopy_kernel(loopy_knl)

    @property
    def is_complex_valued(self):
        return self.kernel.is_complex_valued

    def get_expression(self, dist_vec):
        return self.kernel.get_expression(dist_vec)

    def postprocess_at_source(self, expr, avec):
        return self.kernel.postprocess_at_source(expr, avec)

    def postprocess_at_target(self, expr, avec):
        return self.kernel.postprocess_at_target(expr, avec)

    def get_scaling(self):
        return self.kernel.get_scaling()

    def transform_to_code(self, expr):
        return self.kernel.transform_to_code(expr)

    def get_args(self):
        return self.kernel.get_args()

    def get_preambles(self):
        return self.kernel.get_preambles()

# }}}


# {{{ derivatives

class KernelPartialDerivative(Expression):
    """A placeholder for a partial derivative of a kernel, to be used
    in the *derivative_expression* arguments of :class:`DirectionalSourceDerivative`
    and :class:`DirectionalTargetDerivative`.
    """

    def __init__(self, axis):
        self.axis = axis

    def __getinitargs__(self):
        return (self.axis,)

    mapper_method = "map_kernel_partial_derivative"


class DerivativeBase(KernelWrapper):
    pass


class AxisTargetDerivative(DerivativeBase):
    def __init__(self, axis, kernel):
        KernelWrapper.__init__(self, kernel)
        self.axis = axis

    def replace_inner_kernel(self, new_inner_kernel):
        return AxisTargetDerivative(new_inner_kernel, self.derivative_expression)

    def postprocess_at_target(self, expr, bvec):
        expr = self.kernel.postprocess_at_target(expr, bvec)
        return expr.diff(bvec[self.axis])


class _VectorIndexPrefixer(IdentityMapper):
    def __init__(self, vec_name, prefix):
        self.vec_name = vec_name
        self.prefix = prefix

    def map_subscript(self, expr):
        from pymbolic.primitives import CommonSubexpression
        if expr.aggregate.name == self.vec_name \
                and isinstance(expr.index, int):
            return CommonSubexpression(expr.aggregate[
                    self.prefix + (expr.index,)])
        else:
            return IdentityMapper.map_subscript(self, expr)


class DirectionalTargetDerivative(DerivativeBase):
    def __init__(self, kernel, derivative_expression):
        """
        :arg derivative_expression: A scalar expression for the directional
            derivative to be computed. :class:`KernelPartialDerivative` is
            used as a placeholder for partial derivatives of the kernel.
        """
        KernelWrapper.__init__(self, kernel)
        self.derivative_expression = derivative_expression

    def replace_inner_kernel(self, new_inner_kernel):
        return DirectionalTargetDerivative(
                new_inner_kernel, self.derivative_expression)

    def transform_to_code(self, expr):
        from sumpy.codegen import VectorComponentRewriter
        vcr = VectorComponentRewriter([self.dir_vec_name])
        from pymbolic.primitives import Variable
        return _VectorIndexPrefixer(self.dir_vec_name, (Variable("itgt"),))(
                vcr(self.kernel.transform_to_code(expr)))

    def postprocess_at_target(self, expr, bvec):
        expr = self.kernel.postprocess_at_target(expr, bvec)

        dimensions = len(bvec)
        assert dimensions == self.dimensions

        from sumpy.symbolic import make_sym_vector
        dir_vec = make_sym_vector(self.dir_vec_name, dimensions)

        # bvec = tgt-center
        return sum(dir_vec[axis]*expr.diff(bvec[axis])
                for axis in range(dimensions))


class DirectionalSourceDerivative(DerivativeBase):
    def __init__(self, kernel, derivative_expression):
        """
        :arg derivative_expression: A scalar expression for the directional
            derivative to be computed. :class:`KernelPartialDerivative` is
            used as a placeholder for partial derivatives of the kernel.
        """

        KernelWrapper.__init__(self, kernel)
        self.derivative_expression = derivative_expression

    def replace_inner_kernel(self, new_inner_kernel):
        return DirectionalSourceDerivative(
                new_inner_kernel, self.derivative_expression)

    def transform_to_code(self, expr):
        from sumpy.codegen import VectorComponentRewriter
        vcr = VectorComponentRewriter([self.dir_vec_name])
        from pymbolic.primitives import Variable
        return _VectorIndexPrefixer(self.dir_vec_name, (Variable("isrc"),))(
                vcr(self.kernel.transform_to_code(expr)))

    def postprocess_at_source(self, expr, avec):
        expr = self.kernel.postprocess_at_source(expr, avec)

        dimensions = len(avec)
        assert dimensions == self.dimensions

        from sumpy.symbolic import make_sym_vector
        dir_vec = make_sym_vector(self.dir_vec_name, dimensions)

        # avec = center-src -> minus sign from chain rule
        return sum(-dir_vec[axis]*expr.diff(avec[axis])
                for axis in range(dimensions))

# }}}

# vim: fdm=marker
