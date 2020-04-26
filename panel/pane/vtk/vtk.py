# coding: utf-8
"""
Defines a VTKPane which renders a vtk plot using VTKPlot bokeh model.
"""
import sys
import json
import base64
import zipfile

try:
    from urllib.request import urlopen
except ImportError: # python 2
    from urllib import urlopen

from six import string_types

import param
import numpy as np

from pyviz_comms import JupyterComm

from .enums import PRESET_CMAPS
from ..base import PaneBase
from ...util import isfile

if sys.version_info >= (2, 7):
    base64encode = lambda x: base64.b64encode(x).decode('utf-8')
else:
    base64encode = lambda x: x.encode('base64')

from bokeh.util.serialization import make_globally_unique_id
from bokeh.models import LinearColorMapper
from abc import abstractmethod

class AbstractVTK(PaneBase):

    __abstract = True
    
    axes = param.Dict(doc="""
        Parameters of the axes to construct in the 3d view.

        Must contain at least ``xticker``, ``yticker`` and ``zticker``.

        A ``ticker`` is a dictionary which contains:
          - ``ticks`` (array of numbers) - required.
              Positions in the scene coordinates of the corresponding
              axis' ticks.
          - ``labels`` (array of strings) - optional.
              Label displayed respectively to the `ticks` positions.
              If `labels` are not defined they are infered from the
              `ticks` array.
          - ``digits``: number of decimal digits when `ticks` are converted to `labels`.
          - ``fontsize``: size in pts of the ticks labels.
          - ``show_grid``: boolean.
                If true (default) the axes grid is visible.
          - ``grid_opacity``: float between 0-1.
                Defines the grid opacity.
          - ``axes_opacity``: float between 0-1.
                Defines the axes lines opacity.
    """)

    camera = param.Dict(doc="""State of the rendered VTK camera.""")

    color_mappers = param.List(doc="""Color mapper of the actor in the scene""")

    orientation_widget = param.Boolean(default=False, doc="""
        Activate/Deactivate the orientation widget display.""")

    def _process_param_change(self, msg):
        msg = super(AbstractVTK, self)._process_param_change(msg)
        if 'axes' in msg and msg['axes'] is not None:
            VTKAxes = getattr(sys.modules['panel.models.vtk'], 'VTKAxes')
            axes = msg['axes']
            msg['axes'] = VTKAxes(**axes)
        return msg

    def _update_model(self, events, msg, root, model, doc, comm):
        if 'axes' in msg and msg['axes'] is not None:
            VTKAxes = getattr(sys.modules['panel.models.vtk'], 'VTKAxes')
            axes = msg['axes']
            if isinstance(axes, dict):
                msg['axes'] = VTKAxes(**axes)
            elif isinstance(axes, VTKAxes):
                msg['axes'] = VTKAxes(**axes.properties_with_values())
        super(AbstractVTK, self)._update_model(events, msg, root, model, doc, comm)

class SyncHelpers:
    """
    Class containing helpers functions to update vtkRenderingWindow
    """

    def make_ren_win(self):
        import vtk
        ren = vtk.vtkRenderer()
        ren_win = vtk.vtkRenderWindow()
        ren_win.AddRenderer(ren)
        return ren_win
    
    def set_background(self, r, g, b):
        self.get_renderer().SetBackground(r, g, b)
        self.synchronize()
    
    def add_actors(self, actors):
        """
        Add a list of `actors` to the VTK renderer
        if `reset_camera` is True, the current camera and it's clipping
        will be reset.
        """
        for actor in actors:
            self.get_renderer().AddActor(actor)
    
    @staticmethod
    def _rgb2hex(r, g, b):
        int_type = (int, np.integer)
        if isinstance(r, int_type) and isinstance(g, int_type) is isinstance(b, int_type):
            return "#{0:02x}{1:02x}{2:02x}".format(r, g, b)
        else:
            return "#{0:02x}{1:02x}{2:02x}".format(
                int(255 * r), int(255 * g), int(255 * b)
            )
    
    def get_color_mappers(self):
        cmaps = []
        for view_prop in self.get_renderer().GetViewProps():
            if view_prop.IsA('vtkScalarBarActor'):
                rgba_arr = np.frombuffer(
                    memoryview(view_prop.GetLookupTable().GetTable()), 
                    dtype=np.uint8
                ).reshape((-1, 4))
                palette = [self._rgb2hex(*rgb) for rgb in rgba_arr[:,:3]]
                low, high = view_prop.GetLookupTable().GetTableRange()
                name = view_prop.GetTitle()
                cmaps.append(
                    LinearColorMapper(low=low, high=high, name=name, palette=palette)
                )
        return cmaps

    def remove_actors(self, actors):
        """
        Add a list of `actors` to the VTK renderer
        if `reset_camera` is True, the current camera and it's clipping
        will be reset.
        """
        for actor in actors:
            self.get_renderer().RemoveActor(actor)
    
    def remove_all_actors(self):
        self.remove_actors(self.actors)

    @property
    def vtk_camera(self):
        return self.get_renderer().GetActiveCamera()

    @vtk_camera.setter
    def vtk_camera(self, camera):
        self.get_renderer().SetActiveCamera(camera)

    @property
    def actors(self):
        return list(self.get_renderer().GetActors())

    @abstractmethod
    def get_renderer(self):
        """
        get the active renderer
        """
    
    @abstractmethod
    def synchronize(self):
        """
        function to synchronize the renderer with the view
        """

    @abstractmethod
    def reset_camera(self):
        """
        Reset the camera
        """

class VTK(AbstractVTK, SyncHelpers):
    """
    VTK panes allow rendering VTK objects.
    Synchronize a vtkRenderWindow constructs on python side
    with a custom bokeh model on javascript side
    """

    enable_keybindings = param.Boolean(default=False, doc="""
        Activate/Deactivate keys binding.

        Warning: These keys bind may not work as expected in a notebook
        context if they interact with already binded keys
    """)

    _one_time_reset = param.Boolean(default=False)

    _updates = True

    _rerender_params = ['object']

    _rename = {'_one_time_reset': 'one_time_reset'}

    @classmethod
    def applies(cls, obj):
        if 'vtk' not in sys.modules:
            return False
        else:
            import vtk
            return isinstance(obj, vtk.vtkRenderWindow)
    
    def __init__(self, object=None, **params):
        if object is None:
            object = self.make_ren_win()
        self._debug_serializer = params.pop('debug_serializer', False)
        super(VTK, self).__init__(object, **params)
        self._contexts = {}
        import panel.pane.vtk.synchronizable_serializer as rws
        rws.initializeSerializers()

    def _get_model(self, doc, root=None, parent=None, comm=None):
        """
        Should return the bokeh model to be rendered.
        """
        if 'panel.models.vtk' not in sys.modules:
            if isinstance(comm, JupyterComm):
                self.param.warning('VTKSynchronizedPlot was not imported on instantiation '
                                   'and may not render in a notebook. Restart '
                                   'the notebook kernel and ensure you load '
                                   'it as part of the extension using:'
                                   '\n\npn.extension(\'vtk\')\n')
            from ...models.vtk import VTKSynchronizedPlot
        else:
            VTKSynchronizedPlot = getattr(sys.modules['panel.models.vtk'], 'VTKSynchronizedPlot')
        import panel.pane.vtk.synchronizable_serializer as rws
        model = VTKSynchronizedPlot()
        context = rws.SynchronizationContext(id_root=make_globally_unique_id(), debug=self._debug_serializer)
        scene, arrays = self._serialize_ren_win(self.object, context)
        color_mappers = self.get_color_mappers()
        props = self._process_param_change(self._init_properties())
        props.update(scene=scene, arrays=arrays, color_mappers=color_mappers)
        model.update(**props)

        if root is None:
            root = model
        self._link_props(model, 
                         ['camera', 'color_mappers', 'enable_keybindings', 'one_time_reset',
                          'orientation_widget'],
                         doc, root, comm)
        self._contexts[model.id] =  context
        self._models[root.ref['id']] = (model, parent)
        return model

    def _cleanup(self, root):
        ref = root.ref['id']
        self._contexts.pop(ref, None)
        super(VTK, self)._cleanup(root)

    def _serialize_ren_win(self, ren_win, context, exclude_arrays=None):
        import panel.pane.vtk.synchronizable_serializer as rws
        if exclude_arrays is None:
            exclude_arrays = []
        ren_win.OffScreenRenderingOn() # to not pop a vtk windows
        ren_win.Modified()
        ren_win.Render()
        scene = rws.serializeInstance(None, ren_win, context.getReferenceId(ren_win), context, 0)
        scene['properties']['numberOfLayers'] = 2 #On js side the second layer is for the orientation widget
        arrays = {name: context.getCachedDataArray(name, compression=True)
                    for name in context.dataArrayCache.keys() 
                    if name not in exclude_arrays}
        return scene, arrays

    def _update(self, model):
        context = self._contexts[model.id]
        scene, arrays = self._serialize_ren_win(
            self.object, 
            context,
            exclude_arrays=model.arrays_processed
        )
        context.checkForArraysToRelease()
        model.update(arrays=arrays)
        model.update(scene=scene)

    def _update_color_mappers(self):
        self.color_mappers = self.get_color_mappers()

    def get_renderer(self):
        """
        Get the vtk Renderer associated to this pane
        """
        return list(self.object.GetRenderers())[0]

    def synchronize(self):
        self.param.trigger('object')

    def link_camera(self, other):
        """
        Associate the camera of an other VTKSynchronized pane to this renderer
        """
        if not isinstance(other, VTK):
            raise TypeError('Only instance of VTKSynchronized class can be linked')
        else:
            self.vtk_camera = other.vtk_camera

    def reset_camera(self):
        self.get_renderer().ResetCamera()
        self._one_time_reset = not self._one_time_reset #trigger event

    def unlink_camera(self):
        """
        Create a fresh vtkCamera instance and set it to the renderer
        """
        import vtk
        old_camera = self.vtk_camera
        new_camera = vtk.vtkCamera()
        self.vtk_camera = new_camera
        if self.camera is not None:
            for k, v in self.camera.items():
                if type(v) is list:
                    getattr(new_camera, 'Set' + k[0].capitalize() + k[1:])(*v)
                else:
                    getattr(new_camera, 'Set' + k[0].capitalize() + k[1:])(v)
        else:
            new_camera.DeepCopy(old_camera)

    def export_scene(self, filename='vtk_scene'):
        if '.' not in filename:
            filename += '.sync'
        root = self.get_root()
        context = self._contexts[root.id]
        scene = root.scene
        hash_keys = context.dataArrayCache

        with zipfile.ZipFile(filename, mode='w') as zf:
            zf.writestr('index.json', json.dumps(scene))
            for name in hash_keys:
                data = context.getCachedDataArray(name, binary=True, compression=False)
                zf.writestr('data/%s' % name, data, zipfile.ZIP_DEFLATED)
        return filename

class VTKVolume(AbstractVTK):

    ambient = param.Number(default=0.2, step=1e-2, doc="""
        Value to control the ambient lighting. It is the light an
        object gives even in the absence of strong light. It is
        constant in all directions.""")

    colormap = param.Selector(default='erdc_rainbow_bright', objects=PRESET_CMAPS, doc="""
        Name of the colormap used to transform pixel value in color.""")

    diffuse = param.Number(default=0.7, step=1e-2, doc="""
        Value to control the diffuse Lighting. It relies on both the
        light direction and the object surface normal.""")

    display_volume = param.Boolean(default=True, doc="""
        If set to True, the 3D respresentation of the volume is
        displayed using ray casting.""")

    display_slices = param.Boolean(default=False, doc="""
        If set to true, the orthgonal slices in the three (X, Y, Z)
        directions are displayed. Position of each slice can be
        controlled using slice_(i,j,k) parameters.""")

    edge_gradient = param.Number(default=0.4, bounds=(0, 1), step=1e-2, doc="""
        Parameter to adjust the opacity of the volume based on the
        gradient between voxels.""")

    interpolation = param.Selector(default='fast_linear', objects=['fast_linear','linear','nearest'], doc="""
        interpolation type for sampling a volume. `nearest`
        interpolation will snap to the closest voxel, `linear` will
        perform trilinear interpolation to compute a scalar value from
        surrounding voxels.  `fast_linear` under WebGL 1 will perform
        bilinear interpolation on X and Y but use nearest for Z. This
        is slightly faster than full linear at the cost of no Z axis
        linear interpolation.""")

    mapper = param.Dict(doc="Lookup Table in format {low, high, palette}")

    max_data_size = param.Number(default=(256 ** 3) * 2 / 1e6, doc="""
        Maximum data size transfert allowed without subsampling""")

    origin = param.Tuple(default=None, length=3, allow_None=True)

    render_background = param.Color(default='#52576e', doc="""
        Allows to specify the background color of the 3D rendering.
        The value must be specified as an hexadecimal color string.""")

    rescale = param.Boolean(default=False, doc="""
        If set to True the colormap is rescaled beween min and max
        value of the non-transparent pixel, otherwise  the full range
        of the pixel values are used.""")

    shadow = param.Boolean(default=True, doc="""
        If set to False, then the mapper for the volume will not
        perform shading computations, it is the same as setting
        ambient=1, diffuse=0, specular=0.""")

    sampling = param.Number(default=0.4, bounds=(0, 1), step=1e-2, doc="""
        Parameter to adjust the distance between samples used for
        rendering. The lower the value is the more precise is the
        representation but it is more computationally intensive.""")

    spacing = param.Tuple(default=(1, 1, 1), length=3, doc="""
        Distance between voxel in each direction""")

    specular = param.Number(default=0.3, step=1e-2, doc="""
        Value to control specular lighting. It is the light reflects
        back toward the camera when hitting the object.""")

    specular_power = param.Number(default=8., doc="""
        Specular power refers to how much light is reflected in a
        mirror like fashion, rather than scattered randomly in a
        diffuse manner.""")

    slice_i = param.Integer(per_instance=True, doc="""
        Integer parameter to control the position of the slice normal
        to the X direction.""")

    slice_j = param.Integer(per_instance=True, doc="""
        Integer parameter to control the position of the slice normal
        to the Y direction.""")

    slice_k = param.Integer(per_instance=True, doc="""
        Integer parameter to control the position of the slice normal
        to the Z direction.""")

    _serializers = {}

    _rename = {'max_data_size': None, 'spacing': None, 'origin': None}

    _updates = True

    def __init__(self, object=None, **params):
        super(VTKVolume, self).__init__(object, **params)
        self._sub_spacing = self.spacing
        self._update()

    @classmethod
    def applies(cls, obj):
        if ((isinstance(obj, np.ndarray) and obj.ndim == 3) or
            any([isinstance(obj, k) for k in cls._serializers.keys()])):
            return True
        elif 'vtk' not in sys.modules:
            return False
        else:
            import vtk
            return isinstance(obj, vtk.vtkImageData)

    def _get_model(self, doc, root=None, parent=None, comm=None):
        """
        Should return the bokeh model to be rendered.
        """
        if 'panel.models.vtk' not in sys.modules:
            if isinstance(comm, JupyterComm):
                self.param.warning('VTKVolumePlot was not imported on instantiation '
                                   'and may not render in a notebook. Restart '
                                   'the notebook kernel and ensure you load '
                                   'it as part of the extension using:'
                                   '\n\npn.extension(\'vtk\')\n')
            from ...models.vtk import VTKVolumePlot
        else:
            VTKVolumePlot = getattr(sys.modules['panel.models.vtk'], 'VTKVolumePlot')

        props = self._process_param_change(self._init_properties())
        volume_data = self._volume_data

        model = VTKVolumePlot(data=volume_data,
                              **props)
        if root is None:
            root = model
        self._link_props(model, ['colormap', 'orientation_widget', 'camera', 'mapper'], doc, root, comm)
        self._models[root.ref['id']] = (model, parent)
        return model

    def _update_object(self, ref, doc, root, parent, comm):
        self._legend = None
        super(VTKVolume, self)._update_object(ref, doc, root, parent, comm)

    def _init_properties(self):
        return {k: v for k, v in self.param.get_param_values()
                if v is not None and k not in [
                    'default_layout', 'object', 'max_data_size', 'spacing', 'origin'
                ]}

    def _get_object_dimensions(self):
        if isinstance(self.object, np.ndarray):
            return self.object.shape
        else:
            return self.object.GetDimensions()

    def _process_param_change(self, msg):
        msg = super(VTKVolume, self)._process_param_change(msg)
        if self.object is not None:
            slice_params = {'slice_i':0, 'slice_j':1, 'slice_k':2}
            for k, v in msg.items():
                sub_dim = self._subsample_dimensions
                ori_dim = self._orginal_dimensions
                if k in slice_params:
                    index = slice_params[k]
                    msg[k] = int(np.round(v * sub_dim[index] / ori_dim[index]))
        return msg

    def _process_property_change(self, msg):
        msg = super(VTKVolume, self)._process_property_change(msg)
        if self.object is not None:
            slice_params = {'slice_i':0, 'slice_j':1, 'slice_k':2}
            for k, v in msg.items():
                sub_dim = self._subsample_dimensions
                ori_dim = self._orginal_dimensions
                if k in slice_params:
                    index = slice_params[k]
                    msg[k] = int(np.round(v * ori_dim[index] / sub_dim[index]))
        return msg

    def _update(self, model=None):
        self._volume_data = self._get_volume_data()
        if self._volume_data is not None:
            self._orginal_dimensions = self._get_object_dimensions()
            self._subsample_dimensions = self._volume_data['dims']
            self.param.slice_i.bounds = (0, self._orginal_dimensions[0]-1)
            self.slice_i = (self._orginal_dimensions[0]-1)//2
            self.param.slice_j.bounds = (0, self._orginal_dimensions[1]-1)
            self.slice_j = (self._orginal_dimensions[1]-1)//2
            self.param.slice_k.bounds = (0, self._orginal_dimensions[2]-1)
            self.slice_k = (self._orginal_dimensions[2]-1)//2
        if model is not None:
            model.data = self._volume_data

    @classmethod
    def register_serializer(cls, class_type, serializer):
        """
        Register a seriliazer for a given type of class.
        A serializer is a function which take an instance of `class_type`
        (like a vtk.vtkImageData) as input and return a numpy array of the data
        """
        cls._serializers.update({class_type:serializer})

    def _volume_from_array(self, sub_array):
        return dict(buffer=base64encode(sub_array.ravel(order='F' if sub_array.flags['F_CONTIGUOUS'] else 'C')),
                    dims=sub_array.shape if sub_array.flags['F_CONTIGUOUS'] else sub_array.shape[::-1],
                    spacing=self._sub_spacing if sub_array.flags['F_CONTIGUOUS'] else self._sub_spacing[::-1],
                    origin=self.origin,
                    data_range=(sub_array.min(), sub_array.max()),
                    dtype=sub_array.dtype.name)

    def _get_volume_data(self):
        if self.object is None:
            return None
        elif isinstance(self.object, np.ndarray):
            return self._volume_from_array(self._subsample_array(self.object))
        else:
            available_serializer = [v for k, v in VTKVolume._serializers.items() if isinstance(self.object, k)]
            if not available_serializer:
                import vtk
                from vtk.util import numpy_support

                def volume_serializer(inst):
                    imageData = inst.object
                    array = numpy_support.vtk_to_numpy(imageData.GetPointData().GetScalars())
                    dims = imageData.GetDimensions()[::-1]
                    inst.spacing = imageData.GetSpacing()[::-1]
                    inst.origin = imageData.GetOrigin()
                    return inst._volume_from_array(inst._subsample_array(array.reshape(dims, order='C')))

                VTKVolume.register_serializer(vtk.vtkImageData, volume_serializer)
                serializer = volume_serializer
            else:
                serializer = available_serializer[0]
            return serializer(self)

    def _subsample_array(self, array):
        original_shape = array.shape
        spacing = self.spacing
        extent = tuple((o_s - 1) * s for o_s, s in zip(original_shape, spacing))
        dim_ratio = np.cbrt((array.nbytes / 1e6) / self.max_data_size)
        max_shape = tuple(int(o_s / dim_ratio) for o_s in original_shape)
        dowsnscale_factor = [max(o_s, m_s) / m_s for m_s, o_s in zip(max_shape, original_shape)]

        if any([d_f > 1 for d_f in dowsnscale_factor]):
            try:
                import scipy.ndimage as nd
                sub_array = nd.interpolation.zoom(array, zoom=[1 / d_f for d_f in dowsnscale_factor], order=0)
            except ImportError:
                sub_array = array[::int(np.ceil(dowsnscale_factor[0])),
                                  ::int(np.ceil(dowsnscale_factor[1])),
                                  ::int(np.ceil(dowsnscale_factor[2]))]
            self._sub_spacing = tuple(e / (s - 1) for e, s in zip(extent, sub_array.shape))
        else:
            sub_array = array
            self._sub_spacing = self.spacing
        return sub_array


class VTKJS(AbstractVTK):
    """
    VTK panes allow rendering vtk scene stored in a vtkjs.
    """

    enable_keybindings = param.Boolean(default=False, doc="""
        Activate/Deactivate keys binding.

        Warning: These keybindings may not work as expected in a
                 notebook context if they interact with already
                 bound keys.""")

    _serializers = {}

    _updates = True


    def __init__(self, object=None, **params):
        super(VTKJS, self).__init__(object, **params)
        self._vtkjs = None

    @classmethod
    def applies(cls, obj):
        if isinstance(obj, string_types) and obj.endswith('.vtkjs'):
            return True

    def _get_model(self, doc, root=None, parent=None, comm=None):
        """
        Should return the bokeh model to be rendered.
        """
        if 'panel.models.vtk' not in sys.modules:
            if isinstance(comm, JupyterComm):
                self.param.warning('VTKPlot was not imported on instantiation '
                                   'and may not render in a notebook. Restart '
                                   'the notebook kernel and ensure you load '
                                   'it as part of the extension using:'
                                   '\n\npn.extension(\'vtk\')\n')
            from ...models.vtk import VTKJSPlot
        else:
            VTKJSPlot = getattr(sys.modules['panel.models.vtk'], 'VTKJSPlot')

        vtkjs = self._get_vtkjs()
        data = base64encode(vtkjs) if vtkjs is not None else vtkjs
        props = self._process_param_change(self._init_properties())
        model = VTKJSPlot(data=data, **props)
        if root is None:
            root = model
        self._link_props(model, ['camera', 'enable_keybindings', 'orientation_widget'], doc, root, comm)
        self._models[root.ref['id']] = (model, parent)
        return model

    def _get_vtkjs(self):
        if self._vtkjs is None and self.object is not None:
            if isinstance(self.object, string_types) and self.object.endswith('.vtkjs'):
                if isfile(self.object):
                    with open(self.object, 'rb') as f:
                        vtkjs = f.read()
                else:
                    data_url = urlopen(self.object)
                    vtkjs = data_url.read()
            elif hasattr(self.object, 'read'):
                vtkjs = self.object.read()
            self._vtkjs = vtkjs
        return self._vtkjs

    def _update(self, model):
        self._vtkjs = None
        vtkjs = self._get_vtkjs()
        model.data = base64encode(vtkjs) if vtkjs is not None else vtkjs

    def export_vtkjs(self, filename='vtk_panel.vtkjs'):
        with open(filename, 'wb') as f:
            f.write(self._get_vtkjs())
