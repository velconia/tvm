# pylint: disable=invalid-name
"""Schedule for depthwise_conv2d with auto fusion"""
import tvm
from ..nn.util import get_const_tuple

def schedule_depthwise_conv2d_map(op):
    """Schedule for depthwise_conv2d map ops.

    This include scale-shift and relu.

    Parameters
    ----------
    op: Operation
        The symbolic description of the operation, should be depthwise_conv2d or
        depthwise_conv2d followed by a sequence of one-to-one-mapping operators.

    Returns
    -------
    s: Schedule
        The computation schedule for the op.
    """
    s = tvm.create_schedule(op)
    def schedule_depthwise_conv2d(PaddedInput, Filter, DepthwiseConv2d):
        """Schedule for depthwise_conv2d declared in topi.nn.conv"""
        out_shape = get_const_tuple(DepthwiseConv2d.shape)
        out_height = out_shape[2]
        out_width = out_shape[3]
        channel_multiplier = get_const_tuple(Filter.shape)[1]
        s[PaddedInput].compute_inline()
        IS = s.cache_read(PaddedInput, "shared", [DepthwiseConv2d])
        FS = s.cache_read(Filter, "shared", [DepthwiseConv2d])
        IL = s.cache_read(IS, "local", [DepthwiseConv2d])
        FL = s.cache_read(FS, "local", [DepthwiseConv2d])
        if DepthwiseConv2d.op in s.outputs:
            Output = DepthwiseConv2d
            CL = s.cache_write(DepthwiseConv2d, "local")
        else:
            Output = op.output(0)
            s[DepthwiseConv2d].set_scope("local")
        # schedule parameters
        num_thread = 8
        num_vthread_x = 1
        num_vthread_y = 1
        blocking_h = out_height
        blocking_w = out_width
        if out_height % 48 == 0:
            blocking_h = 48
        elif out_height % 32 == 0:
            blocking_h = 32
        if out_width % 48 == 0:
            blocking_w = 48
            num_vthread_y = 3
        elif out_width % 32 == 0:
            blocking_w = 32
        block_x = tvm.thread_axis("blockIdx.x")
        block_y = tvm.thread_axis("blockIdx.y")
        thread_x = tvm.thread_axis((0, num_thread), "threadIdx.x")
        thread_y = tvm.thread_axis((0, num_thread), "threadIdx.y")
        thread_vx = tvm.thread_axis((0, num_vthread_x), "vthread", name="vx")
        thread_vy = tvm.thread_axis((0, num_vthread_y), "vthread", name="vy")
        # split and bind
        bx, bxi = s[Output].split(Output.op.axis[1], factor=channel_multiplier)
        s[Output].reorder(Output.op.axis[2], Output.op.axis[3], bxi)
        bx = s[Output].fuse(Output.op.axis[0], bx)
        s[Output].bind(bx, block_x)
        by1, y1i = s[Output].split(Output.op.axis[2], factor=blocking_h)
        tvx, vxi = s[Output].split(y1i, nparts=num_vthread_x)
        tx, xi = s[Output].split(vxi, nparts=num_thread)
        by2, y2i = s[Output].split(Output.op.axis[3], factor=blocking_w)
        tvy, vyi = s[Output].split(y2i, nparts=num_vthread_y)
        ty, yi = s[Output].split(vyi, nparts=num_thread)
        s[Output].reorder(by1, by2, tvx, tvy, tx, ty, xi, yi)
        by = s[Output].fuse(by1, by2)
        s[Output].bind(tvx, thread_vx)
        s[Output].bind(tvy, thread_vy)
        s[Output].bind(tx, thread_x)
        s[Output].bind(ty, thread_y)
        s[Output].bind(by, block_y)
        # local memory load
        s[IL].compute_at(s[Output], ty)
        s[FL].compute_at(s[Output], ty)
        if DepthwiseConv2d.op in s.outputs:
            s[CL].compute_at(s[Output], ty)
        else:
            s[DepthwiseConv2d].compute_at(s[Output], ty)
        # input's shared memory load
        s[IS].compute_at(s[Output], by)
        tx, xi = s[IS].split(IS.op.axis[2], nparts=num_thread)
        ty, yi = s[IS].split(IS.op.axis[3], nparts=num_thread)
        s[IS].bind(tx, thread_x)
        s[IS].bind(ty, thread_y)
        # filter's shared memory load
        s[FS].compute_at(s[Output], by)
        s[FS].reorder(FS.op.axis[2], FS.op.axis[3], FS.op.axis[1])
        tx, xi = s[FS].split(FS.op.axis[2], nparts=num_thread)
        ty, yi = s[FS].split(FS.op.axis[3], nparts=num_thread)
        s[FS].bind(tx, thread_x)
        s[FS].bind(ty, thread_y)

    def traverse(OP):
        # inline all one-to-one-mapping operators except the last stage (output)
        if OP.tag == 'ewise' or OP.tag == 'scale_shift':
            if OP not in s.outputs:
                s[OP].compute_inline()
            for tensor in OP.input_tensors:
                if str(tensor.op.input_tensors) != str([]):
                    traverse(tensor.op)
        # schedule depthwise_conv2d
        if OP.tag == 'depthwise_conv2d':
            PaddedInput = OP.input_tensors[0]
            Filter = OP.input_tensors[1]
            DepthwiseConv2d = OP.output(0)
            schedule_depthwise_conv2d(PaddedInput, Filter, DepthwiseConv2d)

    traverse(op)
    return s