from .pattern import Path, Pattern, Port
from .passive import Box, Waveguide
from ..typing import *
from ..utils import _um_str

from collections import defaultdict

import numpy as np
import gdspy as gy

from shapely.geometry import Polygon, MultiPolygon, Point
from shapely.affinity import rotate, translate
from shapely.ops import cascaded_union
from descartes import PolygonPatch
import trimesh
from trimesh import creation, visual
from copy import deepcopy as copy

try:
    import plotly.graph_objects as go
except ImportError:
    pass
try:
    MEEP_IMPORTED = True
    import meep as mp
except ImportError:
    MEEP_IMPORTED = False
try:
    NAZCA_IMPORTED = True
    import nazca as nd
except ImportError:
    NAZCA_IMPORTED = False


class Multilayer:
    def __init__(self, pattern_to_layer: List[Tuple[Union[Pattern, Path, gy.Polygon, gy.FlexPath, Polygon],
                                                    Union[int, str]]] = None):
        self.pattern_to_layer = [] if pattern_to_layer is None else pattern_to_layer
        self.layer_to_pattern, self.port = self._init_multilayer()

    @classmethod
    def from_pattern(cls, pattern: Pattern, layer: str):
        return cls([(pattern, layer)])

    @classmethod
    def from_nazca_cell(cls, cell: "nd.Cell"):
        """

        Args:
            cell: Nazca cell to get Multilayer

        Returns:

        """
        # a glimpse into cell_iter()
        # code from https://nazca-design.org/forums/topic/clipping-check-distance-and-length-for-interconnects/
        multilayers = defaultdict(list)
        for named_tuple in nd.cell_iter(cell, flat=True):
            if named_tuple.cell_start:
                for polygon, points, bbox in named_tuple.iters['polygon']:
                    if polygon.layer == 'bb_pin':
                        continue
                    # fixing point definitions from mask to 1nm prcesision,
                    # kinda hacky but is physical and prevents false polygons
                    points = np.around(points, decimals=3)
                    multilayers[polygon.layer].append(Pattern(Polygon(points)))
        return cls([(Pattern(*pattern_list), layer) for layer, pattern_list in multilayers.items()])

    @property
    def bounds(self) -> Dim4:
        """Bounding box

        Returns:
            Bounding box for the component of the form (minx, maxx, miny, maxy)

        """
        bbox = self.gdspy_cell().get_bounding_box()
        return bbox[0][0], bbox[0][1], bbox[1][0], bbox[1][1]

    @property
    def size(self) -> Dim2:
        """Size of the pattern

        Returns:
            Tuple of the form :code:`(sizex, sizey)`

        """
        b = self.bounds  # (minx, miny, maxx, maxy)
        return b[2] - b[0], b[3] - b[1]  # (maxx - minx, maxy - miny)

    @property
    def center(self) -> Dim2:
        """

        Returns:
            Center for the component

        """
        b = self.bounds  # (minx, miny, maxx, maxy)
        return (b[2] + b[0]) / 2, (b[3] + b[1]) / 2  # (avgx, avgy)

    def align(self, c: Union["Pattern", Tuple[float, float]]) -> "Multilayer":
        """Align center of pattern

        Args:
            c: A pattern (align to the pattern's center) or a center point for alignment

        Returns:
            Aligned pattern

        """
        old_x, old_y = self.center
        center = c if isinstance(c, tuple) else c.center
        self.translate(center[0] - old_x, center[1] - old_y)
        return self

    def translate(self, dx: float = 0, dy: float = 0) -> "Multilayer":
        """Translate the multilayer by translating all of the patterns within it individually

        Args:
            dx: translation in x
            dy: translation in y

        Returns:

        """
        for pattern, _ in self.pattern_to_layer:
            pattern.translate(dx, dy)
        self.layer_to_pattern, _ = self._init_multilayer()
        for name, port in self.port.items():
            self.port[name] = Port(port.x + dx, port.y + dy, port.a)
        return self

    def rotate(self, angle: float, origin: str = (0, 0)) -> "Multilayer":
        """Rotate the multilayer by rotating all of the patterns within it individually

        Args:
            angle: rotation angle in degrees
            origin: origin of rotation

        Returns:
            Rotated pattern

        """
        for pattern, _ in self.pattern_to_layer:
            pattern.rotate(angle, origin)
        self.layer_to_pattern, _ = self._init_multilayer()
        port_to_point = {name: rotate(Point(*port.xy), angle, origin) for name, port in self.port.items()}
        self.port = {name: Port(float(point.x), float(point.y), self.port[name].a + angle / 180 * np.pi)
                     for name, point in port_to_point.items()}
        return self

    def flip(self, center: Tuple[float, float] = (0, 0), horiz: bool = False) -> "Multilayer":
        """Flip the multilayer about center (vertical, or about x-axis, by default)

        Args:
            center: center about which to flip
            horiz: flip horizontally instead of vertically

        Returns:
            Flipped pattern

        """
        for pattern, _ in self.pattern_to_layer:
            pattern.flip(center, horiz)
        self.layer_to_pattern, _ = self._init_multilayer()
        return self

    def to(self, port: Port,  port_name: Optional[str] = None):
        if port_name is None:
            return self.rotate(port.a_deg).translate(port.x, port.y)
        else:
            return self.rotate(port.a_deg - self.port[port_name].a_deg + 180, origin=self.port[port_name].xy).translate(
                port.x - self.port[port_name].x, port.y - self.port[port_name].y
            )

    @property
    def copy(self) -> "Multilayer":
        """Return a copy of this layer for repeated use

        Returns:
            A deep copy of this layer

        """
        return copy(self)

    def gdspy_cell(self, cell_name: str = 'dummy') -> gy.Cell:
        """

        Args:
            cell_name: Cell name

        Returns:
            A GDSPY cell

        """
        cell = gy.Cell(cell_name, exclude_from_current=(cell_name == 'dummy'))
        for pattern, layer in self._pattern_to_layer.items():
            for poly in pattern.polys:
                cell.add(gy.Polygon(np.asarray(poly.exterior.coords.xy).T, layer=layer))
        return cell

    def nazca_cell(self, cell_name: str, callback: Optional[Callable] = None) -> "nd.Cell":
        """Turn this multilayer into a Nazca cell

        Args:
            cell_name: Cell name
            callback: Callback function to call using Nazca (adding pins, other structures)

        Returns:
            A Nazca cell
        """
        if not NAZCA_IMPORTED:
            raise ImportError('Nazca not installed! Please install nazca prior to running nazca_cell().')
        with nd.Cell(cell_name) as cell:
            for pattern, layer in self._pattern_to_layer.items():
                for poly in pattern.polys:
                    nd.Polygon(points=np.around(np.asarray(poly.exterior.coords.xy).T, decimals=3),
                               layer=layer).put()
            for name, port in self.port.items():
                nd.Pin(name).put(*port.xya_deg)
            if callback is not None:
                callback()
            nd.put_stub()
        return cell

    def meep_geometry(self, layer_to_zrange: Dict[str, Tuple[float, float]],
                      process_extrusion: Dict[str, List[Tuple[str, str, str]]] = None,
                      layer_to_material: Dict = None, dimensions: int = 3) -> Dict:
        """Turns this multilayer into a  dictionary of meep geometry objects
        Args:
            layer_to_zrange: a dictionary of z-positions that describe thicknesses or depths for named process layers
            process_extrusion: a dictionary of steps that define the processing steps
                for the masks described in the multilayer
            layer_to_material: a dictionary mapping the layer to the corresponding meep material
            dimensions: the dimensionality of the meep FDTD simulation to be run.
                - If dimensions=3: The geometry is returned in the expected x,y,z coordinates where the
                z direction is the axis of extrusion.
                - If dimensions=2: y and z are swapped, such that geometry extrusion is in the y direction.
                This allows for slab waveguide simulations using xy plane that conform to meep definition of a 2D simulation
                - If dimensions=1: Not yet implemented

        Returns:
            Dictionary with layers as keys to lists of meep geomtery objects
        """

        # Need a way to rotate the coordinates for meep dimensionality, 1D -> Z, 2D -> XY, 3D, XYZ
        if dimensions == 1:
            # TODO: Make sense of this case and for a PIC and then implement
            raise ValueError('Not implemented. Run a simpler simulation for now')
        elif dimensions == 2:
            axis_extrusion = mp.Vector3(0, 1, 0)

            def meep_coord(x, y):
                return mp.Vector3(x, 0, y)
        else:
            axis_extrusion = mp.Vector3(0, 0, 1)
            def meep_coord(x, y):
                return mp.Vector3(x, y, 0)

        def _pattern_to_prisms(pattern, material, zrange):
            zmin, zmax = zrange
            meep_geom_list = []
            for poly in pattern:
                # normally is returned as a row of xs and then a row of ys
                prism_pts = [meep_coord(x, y) for x, y in np.asarray(poly.exterior.coords.xy).T[1:, :]]
                meep_geom_list.append(mp.Prism(prism_pts, height=(zmax - zmin), axis=axis_extrusion,
                                               material=material).shift(zmin * axis_extrusion))
                # overwrite with air material if interiors
                if list(poly.interiors):
                    for inter in list(poly.interiors):
                        prism_interior_pts = [meep_coord(x, y) for x, y in np.asarray(inter.coords.xy).T[1:, :]]
                        meep_geom_list.append(mp.Prism(prism_interior_pts, height=(zmax - zmin), axis=axis_extrusion,
                                                       material=mp.air).shift(zmin * axis_extrusion))
                # skip the first pt, meep doesn't like repeated pts like shapely
            return meep_geom_list

        meep_geometries = defaultdict(list)
        # TODO: This structure is repeated a lot, there must be a more concise way to handle this. There is basically two iterators that are just slightly different

        if process_extrusion is not None:
            layer_to_extrusion = self._build_layers(layer_to_zrange=layer_to_zrange,
                                                    process_extrusion=process_extrusion)
            for layer, pattern_zrange in layer_to_extrusion.items():
                layer = layer.split('_')[0]
                if layer in layer_to_material.keys():
                    pattern, zrange = pattern_zrange
                    material = layer_to_material[layer]
                    meep_geometries[layer] += _pattern_to_prisms(pattern, material, zrange)
        else:
            for layer, pattern in self.layer_to_pattern.items():
                if layer in layer_to_material.keys():
                    material = layer_to_material[layer]
                    try:
                        zrange = layer_to_zrange[layer]
                    except KeyError:
                        raise KeyError(f"No zranges given for the layer {layer}")
                    meep_geometries[layer] = _pattern_to_prisms(pattern, material, zrange)

        return dict(meep_geometries)

    def _init_multilayer(self) -> Tuple[Dict[Union[int, str], MultiPolygon], Dict[str, Port]]:
        self._pattern_to_layer = {comp: layer if isinstance(comp, Pattern) else Pattern(comp)
                                  for comp, layer in self.pattern_to_layer}
        layer_to_polys = defaultdict(list)
        for component, layer in self._pattern_to_layer.items():
            layer_to_polys[layer].extend(component.polys)
        pattern_dict = {layer: MultiPolygon(polys) for layer, polys in layer_to_polys.items()}
        # TODO: temporary way to assign ports
        port = dict(
            sum(
                (
                    list(pattern.port.items())
                    for pattern, _ in self.pattern_to_layer
                ),
                [],
            )
        )

        return pattern_dict, port

    def add(self, pattern: Pattern, layer: str):
        """Add pattern, layer to Multilayer

        Args:
            pattern: :code:`Pattern` to add
            layer: Layer to incorporate :code:`Pattern`

        Returns:

        """
        self.pattern_to_layer.append((pattern, layer))
        self.layer_to_pattern, self.port = self._init_multilayer()

    def plot(self, ax, layer_to_color: Dict[Union[int, str], Union[Dim3, str]], alpha: float = 0.5):
        """

        Args:
            ax:
            layer_to_color:
            alpha:

        Returns:

        """
        for layer, pattern in self.layer_to_pattern.items():
            ax.add_patch(PolygonPatch(pattern, facecolor=layer_to_color[layer], edgecolor='none', alpha=alpha))
        b = self.bounds
        ax.set_xlim((b[0], b[2]))
        ax.set_ylim((b[1], b[3]))
        ax.set_aspect('equal')

    @ property
    def size(self) -> Dim2:
        """Size of the pattern

        Returns:
            Tuple of the form :code:`(sizex, sizey)`

        """
        b = self.bounds  # (minx, miny, maxx, maxy)
        return b[2] - b[0], b[3] - b[1]  # (maxx - minx, maxy - miny)

    def to_trimesh_dict(self, layer_to_zrange: Dict[str, Tuple[float, float]],
                        process_extrusion: Optional[Dict[str, List[Tuple[str, str, str]]]] = None,
                        layer_to_color: Optional[Dict[str, str]] = None, engine: str = 'scad',
                        include_oxide: bool = True):
        if include_oxide and 'oxide' not in self.layer_to_pattern:
            print('WARNING: oxide not included, so adding to multilayer')
            self.pattern_to_layer.append((Box(self.size).align(self.center), 'oxide'))
            self.layer_to_pattern, self.port = self._init_multilayer()
        meshes = {}  # The initialization for the dictionary of all trimesh meshes

        # TODO(sunil): start using logging rather than printing
        def _add_trimesh_layer(pattern, zrange, layer):
            zmin, zmax = zrange
            layer = layer.split('_')[0]
            layer_meshes = []
            for poly in pattern:
                try:
                    layer_meshes.append(trimesh.creation.extrude_polygon(poly, height=zmax - zmin).apply_translation((0, 0, zmin)))
                except IndexError:
                    print('WARNING: bad polygon, skipping')
            layer_meshes = layer_meshes + [meshes[layer]] if layer in meshes.keys() else layer_meshes
            mesh = trimesh.util.concatenate(layer_meshes)
            mesh.visual.face_colors = visual.random_color() if layer_to_color is None else layer_to_color[layer]
            meshes[layer] = mesh

        if process_extrusion is not None:
            # layer_to_extrusion = self._build_layers(layer_to_zrange, process_extrusion)
            layer_to_extrusion = self._build_layers_v0(layer_to_zrange, process_extrusion)
            for layer, pattern_zrange in layer_to_extrusion.items():
                pattern, zrange = pattern_zrange
                _add_trimesh_layer(pattern, zrange, layer)
            return meshes
        else:
            for layer, pattern in self.layer_to_pattern.items():
                try:
                    _add_trimesh_layer(pattern, layer_to_zrange[layer], layer)
                except KeyError:
                    print(f"No zranges given for the layer {layer}")
            return meshes

    def to_stls(self, prefix: str, layer_to_zrange: Dict[str, Tuple[float, float]],
                process_extrusion: Optional[Dict[str, List[Tuple[str, str, str]]]] = None,
                layer_to_color: Optional[Dict[str, str]] = None, engine: str = 'scad',
                layers: Optional[List[str]] = None,
                include_oxide: bool = True,):    # TODO:simplify how filled oxide/material should be handled with/without process extrsusions
        """Exports layer by layer stls representing a psuedo fabrication of the multilayer"
            Args:
                prefix: A string prepending the output stl layers. "{prefix}_{layer}.stl"
                layer_to_zrange: a dictionary of z-positions that describe thicknesses or depths for named process layers
                process_extrusion: a dictionary of steps that define the processing steps for the masks described in the multilayer
                layer_to_color: An optinal dictionary for  specifying the colors of each layer
                engine: A str identifying which backend trimesh should use for generating the stls 
                layers: An optional list for specifying the layers to export to stls. By default all layers are exported
                include_oxide: a boolean to add an addtional block of oxide that fill the 3D space of the psuedo fabrication
        """
        meshes = self.to_trimesh_dict(layer_to_zrange, process_extrusion, layer_to_color, engine, include_oxide)
        for layer, mesh in meshes.items():
            if layers is None or layers and layer in layers:
                mesh.export(f'{prefix}_{layer}.stl')

    def to_trimesh_scene(self, layer_to_zrange: Dict[str, Tuple[float, float]],
                         process_extrusion: Optional[Dict[str, List[Tuple[str, str, str]]]] = None,
                         layer_to_color: Optional[Dict[str, str]] = None, ignore_layers: Optional[List[str]] = None,
                         engine: str = 'scad'):
        meshes = self.to_trimesh_dict(layer_to_zrange, process_extrusion, layer_to_color, engine)
        scene = trimesh.Scene()
        ignore_layers = [] if ignore_layers is None else ignore_layers
        for mesh_name, mesh in meshes.items():
            if mesh_name not in ignore_layers:
                scene.add_geometry(mesh, mesh_name)
        return scene

    def show(self, layer_to_zrange: Dict[str, Tuple[float, float]],
             process_extrusion: Optional[Dict[str, List[Tuple[str, str, str]]]] = None,
             layer_to_color: Optional[Dict[str, str]] = None, ignore_layers: Optional[List[str]] = None,
             engine: str = 'scad'):
        self.to_trimesh_scene(layer_to_zrange, process_extrusion, layer_to_color, ignore_layers, engine).show()

    def _conformal_dilation(self, sorted_heights, thickness, topo_map_dict, raising_pattern, bounding_box):
        # The strategy is to dilate everything, then pairwise difference by height so the tallest is unobstructed.
        # Finally, apply the raising_pattern which is based on the current mask layer
        for count, infr_h in enumerate(sorted_heights):
            for supr_h in sorted_heights:
                if count == 0:
                    # dilation, should only occur in not etched regions, need to filter with raisd_pattern and union
                    dilated_pattern = topo_map_dict[_um_str(supr_h)].offset(grow_d=thickness).intersection(raising_pattern)
                    topo_map_dict[_um_str(supr_h)] = dilated_pattern.union(topo_map_dict[_um_str(supr_h)])
                elif supr_h > infr_h:  # only looking at the necessary lower triangle of the matrix of layers
                    #  moving boudaries for shape dilation and hole shrinkage.
                    #  The infr_h pattern shrinks based on the dilation of the supr_h pattern
                    topo_map_dict[_um_str(infr_h)] = Pattern(topo_map_dict[_um_str(infr_h)]).difference(
                        Pattern(topo_map_dict[_um_str(supr_h)]))
            topo_map_dict[_um_str(infr_h)] = bounding_box.intersection(topo_map_dict[_um_str(infr_h)])
        return topo_map_dict

    def _raise_surface(self, sorted_heights, thickness, topo_map_dict, raising_pattern, bounding_box):
        for elevation in sorted_heights:
            # material added
            elevation_str = _um_str(elevation)
            raised = Pattern(raising_pattern).intersection(
                Pattern(topo_map_dict[elevation_str])).shapely
            # new matrial fully etched
            not_raised = Pattern(topo_map_dict[elevation_str]).difference(
                (Pattern(raised))).shapely
            topo_map_dict[elevation_str] = Pattern(*not_raised)
            if _um_str(elevation + thickness) in topo_map_dict.keys():
                topo_map_dict[_um_str(elevation + thickness)] = topo_map_dict[_um_str(elevation + thickness)].union(Pattern(*raised))
            else:
                topo_map_dict[_um_str(elevation + thickness)] = Pattern(*raised)
        return topo_map_dict

    def _update_layer_to_extrusion(self, layer, heights, topo_map_dict, old_topo_map_dict, layer_to_extrusion):
        for old_elevation in heights:
            for new_elevation in topo_map_dict.keys():
                if float(old_elevation) >= float(new_elevation):
                    continue
                extrusion_zrange = (float(old_elevation), float(new_elevation))
                extrusion_pattern = Pattern(topo_map_dict[new_elevation], call_union=False).intersection(Pattern(old_topo_map_dict[old_elevation], call_union=False)).shapely
                new_layer = f'{layer}_{_um_str(old_elevation)}_{_um_str(new_elevation)}'
                if extrusion_pattern.geoms:
                    pattern_shapely = cascaded_union(extrusion_pattern.geoms)
                    pattern_shapely = MultiPolygon([pattern_shapely]) if isinstance(pattern_shapely, Polygon) else pattern_shapely
                    layer_to_extrusion[new_layer] = (pattern_shapely, extrusion_zrange)
        return layer_to_extrusion

    def _build_layers_v0(self, layer_to_zrange: Dict[str, Tuple[float, float]],
                         process_extrusion: Dict[str, List[Tuple[str, str, str]]]):
        layer_to_extrusion = {}
        layers = self.layer_to_pattern.keys()
        layer_to_pattern_processed = self.layer_to_pattern.copy()

        for step, operations in process_extrusion.items():
            for layer_relation in operations:
                layer, other_layer, operation = layer_relation
                if 'dope' in step and operation == 'intersection':
                    # make a new layer for each doping intersection
                    zmin, zmax = layer_to_zrange[other_layer]
                    z0, z1 = layer_to_zrange[layer]
                    # TODO(): how to deal with different depth doping currently not addressed
                    new_zrange = (max(zmax - (z1 - z0), zmin), zmax)
                else:
                    new_zrange = layer_to_zrange[layer]
                if layer in layers:
                    if other_layer in layers:
                        pattern = Pattern(layer_to_pattern_processed[layer]).boolean_operation(
                            Pattern(layer_to_pattern_processed[other_layer]), operation
                        ).shapely
                    else:
                        pattern = layer_to_pattern_processed[layer]
                    if pattern.geoms:
                        pattern_shapely = cascaded_union(pattern.geoms)
                        pattern_shapely = MultiPolygon([pattern_shapely]) if isinstance(pattern_shapely, Polygon) else pattern_shapely
                        layer_to_pattern_processed[layer] = pattern
                        layer_to_extrusion[layer] = (pattern_shapely, new_zrange)
        return layer_to_extrusion

    def _build_layers(self, layer_to_zrange: Dict[str, Tuple[float, float]],
                      process_extrusion: Dict[str, List[Tuple[str, str, str]]]):
        """Creates a new dictionary called layer_to_extrusion that describes a 2D map of patterns, and heights for extrusion, that respresent the results of a psuedo fabrication based on process extrusion
            Args:
                layer_to_zrange: a dictionary of z-positions that describe thicknesses or depths for named process layers
                process_extrusion: a dictionary of steps that define the processing steps for the masks described in the multilayer

            Returns:
                Dictionary with layers as keys to lists of meep geomtery objects
        """
        layer_to_extrusion = {}
        layer_to_pattern_processed = self.layer_to_pattern.copy()
        layers = layer_to_pattern_processed.keys()

        # building topographic maps for each process step
        minx, miny, maxx, maxy = self.bounds
        bounding_box = Pattern(gy.Polygon(
            [(minx, miny), (minx, maxy),
             (maxx, maxy), (maxx, miny)]))
        topo_map_dict = {_um_str(0): bounding_box}  # zero is the starting height

        # TODO: remove topo_evolution, currently just a debugging tool
        self.topo_evolution = []  # use this to see the topographic map evolutions

        for step, operations in process_extrusion.items():
            for layer_relation in operations:
                layer, other_layer, operation = layer_relation
                try:
                    new_zrange = layer_to_zrange[layer]
                except KeyError:
                    print(f"No zranges given for the layer {layer}")
                if layer in layers:
                    if other_layer in layers:
                        # TODO(Nate): Unnecessary instances. The string boolean operation forces this
                        #  Pattern(Multipolygon).boolean_opertaion(Pattern(Multipolygon)).shapely
                        # It is clear that the Pattern() is only providing a lookup dictionary
                        # but the actual input/output/operation is with Multipolygons.
                        # May be part of a larger codebase adjustment
                        pattern = Pattern(layer_to_pattern_processed[layer]).boolean_operation(
                            Pattern(layer_to_pattern_processed[other_layer]), operation
                        ).shapely
                        layer_to_pattern_processed[layer] = pattern
                    else:
                        pattern = layer_to_pattern_processed[layer]

                    if 'dope' in step and operation == 'intersection':
                        # make a new layer for each doping intersection
                        zmin, zmax = layer_to_zrange[other_layer]
                        # TODO(): how to deal with different depth doping currently not addressed
                        new_zrange = (max(zmax - (new_zrange[1] - new_zrange[0]), zmin), zmax)

                    if 'etch' in step:
                        raising_pattern = bounding_box.difference(Pattern(pattern))
                    else:
                        raising_pattern = Pattern(pattern)

                    # Saving and sorting variables for use in the conformal deposition steps
                    thickness = new_zrange[1] - new_zrange[0]
                    heights = list(topo_map_dict.keys())
                    sorted_heights = list(map(float, heights))
                    sorted_heights.sort(reverse=True)
                    old_topo_map_dict = topo_map_dict.copy()

                    if 'conformal' in step:
                        topo_map_dict = self._conformal_dilation(sorted_heights, thickness, topo_map_dict, raising_pattern, bounding_box)

                    topo_map_dict = self._raise_surface(sorted_heights, thickness, topo_map_dict, raising_pattern, bounding_box)

                    # Using the current topographic map and the previous topographic map to make extrusions
                    # TODO: note in a cmp/doping/trench process steps need to be captured well in this
                    layer_to_extrusion = self._update_layer_to_extrusion(layer, heights, topo_map_dict, old_topo_map_dict, layer_to_extrusion)

                    # TODO: for debugging, need to remove
                    topo_list = [(pattern, layer) for layer, pattern in topo_map_dict.items()]
                    self.topo_evolution.append(Multilayer(topo_list))
        self.topo = Multilayer(topo_list)

        return layer_to_extrusion

    def fill_material(self, layer_name: str, growth: float, centered_layer: str = None):
        all_patterns = [Pattern(poly) for layer, poly in self.layer_to_pattern.items()]
        all_patterns = Pattern(*all_patterns)
        minx, miny, maxx, maxy = all_patterns.bounds
        centered_pattern = all_patterns if centered_layer is None else Pattern(self.layer_to_pattern[centered_layer])
        fill = Pattern(gy.Polygon(
            [(minx - growth / 2, miny - growth / 2), (minx - growth / 2, maxy + growth / 2),
             (maxx + growth / 2, maxy + growth / 2), (maxx + growth / 2, miny - growth / 2)]
        )).align(centered_pattern)
        self.pattern_to_layer.append((fill, layer_name))
        self._pattern_to_layer = {comp: layer if isinstance(comp, Pattern) else Pattern(comp)
                                  for comp, layer in self.pattern_to_layer}
        self.layer_to_pattern, self.port = self._init_multilayer()
        return [(fill, layer_name)]

    @classmethod
    def aggregate(cls, multilayers: List["Multilayer"]):
        return cls(sum((m.pattern_to_layer for m in multilayers), []))


class MultilayerPath(Multilayer):
    def __init__(self, waveguide_w: float, sequence: List[Union[Multilayer, Pattern, float]], path_layer: str):
        """Multilayer path for appending a linear sequence of elements end-to-end (based on port 0)

        Args:
            waveguide_w: Waveguide width
            sequence: Sequence
            path_layer: Path layer
        """
        patterns = []
        if not len(sequence):
            raise ValueError('Require a nonzero multilayer sequence length')
        port = None
        for p in sequence:
            if p is not None:
                d = p if isinstance(p, (Multilayer, Pattern)) else Waveguide(waveguide_w, p)
                if port is None:
                    patterns.append(d.to(Port(0, 0), 'a0'))
                else:
                    patterns.append(d.to(port, 'a0'))
                port = d.port['b0']
        pattern_to_layer = sum(
            (
                [(p, path_layer)] if isinstance(p, Pattern) else p.pattern_to_layer
                for p in patterns
            ),
            [],
        )

        super(MultilayerPath, self).__init__(pattern_to_layer)
        self.port['a0'] = Port(0, 0, -np.pi)
        self.port['b0'] = port
        self.patterns = patterns
        self.sequence = sequence
        self.waveguide_w = waveguide_w
        self.path_layer = path_layer

    @property
    def wg_path(self):
        return Pattern(*[p.wg_path for p in self.patterns], call_union=False)

    def append(self, element: Union[Multilayer, Pattern, float]):
        self.__init__(self.waveguide_w, self.sequence + [element], self.path_layer)
        return self


class Grating(Multilayer):
    def __init__(self, box_dim: Dim2, pitch: float, duty_cycle: float, fill: str, etch: str):
        self.stripe_w = pitch * duty_cycle
        self.pitch = pitch
        self.duty_cycle = duty_cycle

        super(Grating, self).__init__([(Box(box_dim), fill),
                                       (Box(box_dim).striped(self.stripe_w, (pitch, 0)), etch)])


class Via(Multilayer):
    def __init__(self, via_dim: Dim2, boundary_grow: Union[float, Dim2], metal: Union[str, List[str]],
                 via: Union[str, List[str]], pitch: float = 0, shape: Optional[Shape2] = None):
        """Via / metal multilayer stack (currently all params should be specified to 2 decimal places)

        Args:
            via_dim: dimensions of the via (or each via in via array)
            boundary_grow: boundary growth around the via or via array
            metal: Metal layers
            via: Via layers
            pitch: Pitch of the vias (center to center)
            shape: Shape of the array (rows, cols)
        """
        self.via_dim = via_dim
        metal = (metal,) if isinstance(metal, str) else metal
        boundary_grow = boundary_grow if isinstance(boundary_grow, tuple) else tuple([boundary_grow] * len(metal))
        self.boundary_grow = boundary_grow
        self.metal = metal
        self.via = via
        self.pitch = pitch
        self.shape = shape
        self.config = copy(self.__dict__)

        max_boundary_grow = max(boundary_grow)
        via_pattern = Box(via_dim, decimal_places=2)
        if pitch > 0 and shape is not None:
            x, y = np.meshgrid(np.arange(shape[0]) * pitch, np.arange(shape[1]) * pitch)
            patterns = [
                via_pattern.copy.translate(x, y)
                for x, y in zip(x.flatten(), y.flatten())
            ]

            via_pattern = Pattern(*patterns, decimal_places=2)
        boundary = Box((via_pattern.size[0] + 2 * max_boundary_grow,
                        via_pattern.size[1] + 2 * max_boundary_grow), decimal_places=2).align((0, 0)).halign(0)
        via_pattern.align(boundary)
        layers = []
        if isinstance(via, list):
            layers += [(via_pattern.copy, layer) for layer in via]
        elif isinstance(via, str):
            layers += [(via_pattern, via)]
        layers += [(Box((via_pattern.size[0] + 2 * bg,
                         via_pattern.size[1] + 2 * bg), decimal_places=2).align(boundary), layer)
                   for layer, bg in zip(metal, boundary_grow)]
        super(Via, self).__init__(layers)
        self.port['a0'] = Port(self.bounds[0], 0, np.pi)
        self.port['b0'] = Port(self.bounds[2], 0)
