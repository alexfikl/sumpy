from __future__ import division, absolute_import

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

import six
from six.moves import range

import numpy as np
import loopy as lp
import itertools

from sumpy.tools import KernelComputation, KernelCacheWrapper


__doc__ = """

Particle-to-particle
--------------------

.. autoclass:: P2PBase
.. autoclass:: P2P
.. autoclass:: P2PFromCSR

"""


# LATER:
# - Optimization for source == target (postpone)

# {{{ p2p base class

class P2PBase(KernelComputation, KernelCacheWrapper):
    def __init__(self, ctx, kernels, exclude_self, strength_usage=None,
            value_dtypes=None,
            options=[], name=None, device=None):
        """
        :arg kernels: list of :class:`sumpy.kernel.Kernel` instances
        :arg strength_usage: A list of integers indicating which expression
          uses which source strength indicator. This implicitly specifies the
          number of strength arrays that need to be passed.
          Default: all kernels use the same strength.
        """
        KernelComputation.__init__(self, ctx, kernels, strength_usage,
                value_dtypes,
                name, options, device)

        self.exclude_self = exclude_self

        from pytools import single_valued
        self.dim = single_valued(knl.dim for knl in self.kernels)

    def get_loopy_insns_and_result_names(self):
        from sumpy.symbolic import make_sym_vector
        dvec = make_sym_vector("d", self.dim)

        from sumpy.assignment_collection import SymbolicAssignmentCollection
        sac = SymbolicAssignmentCollection()

        result_names = [
                [sac.assign_unique("knl{}_{}".format(i, j),
                    knl.postprocess_at_target(
                        knl.postprocess_at_source(
                            expr,
                            dvec),
                        dvec)
                ) for j, expr in enumerate(knl.get_expressions(dvec))]
                for i, knl in enumerate(self.kernels)
                ]

        retain_names = list(itertools.chain.from_iterable(result_names))

        sac.run_global_cse()

        from sumpy.codegen import to_loopy_insns
        loopy_insns = to_loopy_insns(six.iteritems(sac.assignments),
                vector_names=set(["d"]),
                pymbolic_expr_maps=[
                        knl.get_code_transformer() for knl in self.kernels],
                retain_names=retain_names,
                complex_dtype=np.complex128  # FIXME
                )
        return loopy_insns, result_names

    def get_outputs(self, result_names):
        from pymbolic import var
        exprs = []
        scaling = []
        for i, (knl, results) in enumerate(zip(self.kernels, result_names)):
            for row in range(knl.shape[0]):
                expr = sum(var(results[row * knl.shape[1] + col]) *
                             var("strength").index((self.strength_usage[i][col],
                                                    var("isrc")))
                             for col in range(knl.shape[1]))
                exprs.append(expr)
                scaling.append("knl_{}_scaling".format(row * knl.shape[1]))
        return exprs, scaling

    def get_cache_key(self):
        return (type(self).__name__, tuple(self.kernels), self.exclude_self,
                tuple(tuple(s) for s in self.strength_usage),
                tuple(self.value_dtypes))

# }}}


# {{{ P2P with list of sources and list of targets

class P2P(P2PBase):
    default_name = "p2p"

    def get_kernel(self):
        loopy_insns, result_names = self.get_loopy_insns_and_result_names()

        exprs, scaling = self.get_outputs(result_names)

        if self.exclude_self:
            from pymbolic.primitives import If, Variable
            exprs = [If(Variable("is_self"), 0, expr) for expr in exprs]

        from sumpy.tools import gather_loopy_source_arguments
        loopy_knl = lp.make_kernel(
                "{[isrc,itgt,idim]: 0<=itgt<ntargets and 0<=isrc<nsources \
                        and 0<=idim<dim}",
                self.get_kernel_scaling_assignments()
                + ["""
                for itgt
                    for isrc
                        """] + loopy_insns + ["""
                        <> d[idim] = targets[idim,itgt] - sources[idim,isrc]
                        """] + ["""
                        <> is_self = (isrc == target_to_source[itgt])
                        """ if self.exclude_self else ""] + [
                        lp.Assignment(id=None,
                            assignee="pair_result_%d" % i, expression=expr,
                            temp_var_type=lp.auto)
                        for i, expr in enumerate(exprs)
                        ] + ["""
                    end
                    """] + ["""
                    result[{iknl}, itgt] = {scale} \
                            * simul_reduce(sum, isrc, pair_result_{iknl})
                    """.format(iknl=iknl, scale=scale)
                    for iknl, scale in zip(range(len(exprs)), scaling)] + [
                    ] + ["""
                end
                """],
                [
                    lp.GlobalArg("sources", None,
                        shape=(self.dim, "nsources")),
                    lp.GlobalArg("targets", None,
                        shape=(self.dim, "ntargets")),
                    lp.ValueArg("nsources", None),
                    lp.ValueArg("ntargets", None),
                    lp.GlobalArg("strength", None, shape="nstrengths,nsources"),
                    lp.GlobalArg("result", None,
                        shape="nresults,ntargets", dim_tags="sep,C")
                ] + (
                    [lp.GlobalArg("target_to_source", np.int32, shape=("ntargets",))]
                    if self.exclude_self else []
                ) + gather_loopy_source_arguments(self.kernels),
                name=self.name,
                assumptions="nsources>=1 and ntargets>=1",
                fixed_parameters=dict(
                    dim=self.dim,
                    nstrengths=self.strength_count,
                    nresults=len(exprs)))

        loopy_knl = lp.tag_inames(loopy_knl, "idim*:unr")

        for knl in self.kernels:
            loopy_knl = knl.prepare_loopy_kernel(loopy_knl)

        loopy_knl = lp.tag_array_axes(loopy_knl, "strength", "sep,C")

        return loopy_knl

    def get_optimized_kernel(self, targets_is_obj_array, sources_is_obj_array):
        # FIXME
        knl = self.get_kernel()

        if sources_is_obj_array:
            knl = lp.tag_array_axes(knl, "sources", "sep,C")
        if targets_is_obj_array:
            knl = lp.tag_array_axes(knl, "targets", "sep,C")

        knl = lp.split_iname(knl, "itgt", 1024, outer_tag="g.0")
        return knl

    def __call__(self, queue, targets, sources, strength, **kwargs):
        from pytools.obj_array import is_obj_array
        knl = self.get_cached_optimized_kernel(
                targets_is_obj_array=(
                    is_obj_array(targets) or isinstance(targets, (tuple, list))),
                sources_is_obj_array=(
                    is_obj_array(sources) or isinstance(sources, (tuple, list))))

        return knl(queue, sources=sources, targets=targets, strength=strength,
                **kwargs)

# }}}


# {{{ P2P from CSR-like interaction list

class P2PFromCSR(P2PBase):
    default_name = "p2p_from_csr"

    def get_kernel(self):
        loopy_insns, result_names = self.get_loopy_insns_and_result_names()

        exprs, scaling = self.get_outputs(result_names)

        if self.exclude_self:
            from pymbolic.primitives import If, Variable
            exprs = [If(Variable("is_self"), 0, expr) for expr in exprs]

        from sumpy.tools import gather_loopy_source_arguments
        loopy_knl = lp.make_kernel(
            [
                "{[itgt_box]: 0<=itgt_box<ntgt_boxes}",
                "{[isrc_box]: isrc_box_start<=isrc_box<isrc_box_end}",
                "{[itgt,isrc,idim]: \
                        itgt_start<=itgt<itgt_end and \
                        isrc_start<=isrc<isrc_end and \
                        0<=idim<dim }",
                ],
            self.get_kernel_scaling_assignments()
            + [
                """
                for itgt_box
                    <> tgt_ibox = target_boxes[itgt_box]
                    <> itgt_start = box_target_starts[tgt_ibox]
                    <> itgt_end = itgt_start+box_target_counts_nonchild[tgt_ibox]

                    <> isrc_box_start = source_box_starts[itgt_box]
                    <> isrc_box_end = source_box_starts[itgt_box+1]

                    for isrc_box
                        <> src_ibox = source_box_lists[isrc_box]
                        <> isrc_start = box_source_starts[src_ibox]
                        <> isrc_end = isrc_start+box_source_counts_nonchild[src_ibox]

                        for itgt
                            for isrc
                                <> d[idim] = \
                                        targets[idim,itgt] - sources[idim,isrc] \
                                        {dup=idim}
                                """] + ["""
                                <> is_self = (isrc == target_to_source[itgt])
                                """ if self.exclude_self else ""] + [
                                ] + loopy_insns + [
                                lp.Assignment(id=None,
                                    assignee="pair_result_%d" % i, expression=expr,
                                    temp_var_type=lp.auto)
                                for i, expr in enumerate(exprs)
                                ] + [
                                """
                            end
                            """] +  + ["""
                            result[{iknl}, itgt] = result[{iknl}, itgt] + {scale} \
                                    * simul_reduce(sum, isrc, pair_result_{iknl})
                            """.format(iknl=iknl, scale=scale)
                            for iknl, scale in zip(range(len(exprs)), scaling)] + [
                            ] + ["""
                        end
                    end
                end
                """],
            [
                lp.GlobalArg("box_target_starts,box_target_counts_nonchild,"
                    "box_source_starts,box_source_counts_nonchild,",
                    None, shape=None),
                lp.GlobalArg("source_box_starts, source_box_lists,",
                    None, shape=None),
                lp.GlobalArg("strength", None, shape="nstrengths,nsources"),
                lp.GlobalArg("result", None,
                    shape="nkernels,ntargets", dim_tags="sep,c"),
                lp.GlobalArg("targets", None,
                    shape="dim,ntargets", dim_tags="sep,c"),
                lp.GlobalArg("sources", None,
                    shape="dim,nsources", dim_tags="sep,c"),
                lp.ValueArg("nsources", np.int32),
                lp.ValueArg("ntargets", np.int32),
                "...",
            ] + (
                [lp.GlobalArg("target_to_source", np.int32, shape=("ntargets",))]
                if self.exclude_self else []
            ) + gather_loopy_source_arguments(self.kernels),
            name=self.name, assumptions="ntgt_boxes>=1",
            fixed_parameters=dict(
                dim=self.dim,
                nstrengths=self.strength_count,
                nkernels=len(exprs)*len(self.kernels)))

        loopy_knl = lp.tag_inames(loopy_knl, "idim*:unr")
        loopy_knl = lp.tag_array_axes(loopy_knl, "strength", "sep,C")

        for knl in self.kernels:
            loopy_knl = knl.prepare_loopy_kernel(loopy_knl)

        return loopy_knl

    def get_optimized_kernel(self):
        # FIXME
        knl = self.get_kernel()
        import pyopencl as cl
        dev = self.context.devices[0]
        if dev.type & cl.device_type.CPU:
            knl = lp.split_iname(knl, "itgt_box", 4, outer_tag="g.0")
        else:
            knl = lp.split_iname(knl, "itgt_box", 4, outer_tag="g.0")
        return knl

    def __call__(self, queue, **kwargs):
        knl = self.get_cached_optimized_kernel()

        return knl(queue, **kwargs)

# }}}

# vim: foldmethod=marker
