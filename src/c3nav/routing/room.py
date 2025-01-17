from collections import namedtuple

import numpy as np
from matplotlib.path import Path
from scipy.sparse.csgraph._shortest_path import shortest_path
from scipy.sparse.csgraph._tools import csgraph_from_dense
from shapely.geometry import CAP_STYLE, JOIN_STYLE, LineString
from shapely.ops import cascaded_union

from c3nav.mapdata.utils.geometry import assert_multilinestring, assert_multipolygon
from c3nav.routing.area import GraphArea
from c3nav.routing.connection import GraphConnection
from c3nav.routing.point import GraphPoint
from c3nav.routing.utils.coords import get_coords_angles
from c3nav.routing.utils.mpl import shapely_to_mpl


class GraphRoom():
    def __init__(self, level):
        self.level = level
        self.graph = level.graph

        self.mpl_clear = None

        self.i = None
        self.areas = []
        self.points = None
        self.room_transfer_points = None
        self.distances = np.zeros((1, ))
        self.ctypes = None
        self.excludables = None

    def serialize(self):
        return (
            self.mpl_clear,
            [area.serialize() for area in self.areas],
            self.points,
            self.room_transfer_points,
            self.distances,
            self.ctypes,
            self.excludables,
            self.excludable_points,
        )

    @classmethod
    def unserialize(cls, level, data):
        room = cls(level)
        (room.mpl_clear, areas, room.points, room.room_transfer_points,
         room.distances, room.ctypes, room.excludables, room.excludable_points) = data
        room.areas = tuple(GraphArea(room, *area) for area in areas)
        return room

    # Building the Graph
    def prepare_build(self, geometry):
        self._built_geometry = geometry
        self.clear_geometry = self._built_geometry.buffer(-0.3, join_style=JOIN_STYLE.mitre)

        if self.clear_geometry.is_empty:
            return False

        self._built_points = []
        self._built_is_elevatorlevel = False

        self.mpl_clear = shapely_to_mpl(self.clear_geometry.buffer(0.01, join_style=JOIN_STYLE.mitre))
        self.mpl_stairs = tuple((stair, angle) for stair, angle in self.level.mpl_stairs
                                if self.mpl_clear.intersects_path(stair, filled=True))
        self._built_escalators = tuple(escalator for escalator in self.level._built_escalators
                                       if self.mpl_clear.intersects_path(escalator.mpl_geom.exterior, filled=True))

        self.isolated_areas = []
        return True

    def build_areas(self):
        stairs_areas = self.level.level.geometries.stairs
        stairs_areas = stairs_areas.buffer(0.3, join_style=JOIN_STYLE.mitre, cap_style=CAP_STYLE.flat)
        stairs_areas = stairs_areas.intersection(self._built_geometry)
        self._built_isolated_areas = tuple(assert_multipolygon(stairs_areas))

        escalators_areas = self.level.level.geometries.escalators
        escalators_areas = escalators_areas.intersection(self._built_geometry)
        self._built_isolated_areas += tuple(assert_multipolygon(escalators_areas))

        escalators_and_stairs = cascaded_union((stairs_areas, escalators_areas))

        isolated_areas = tuple(assert_multipolygon(stairs_areas.intersection(self.clear_geometry)))
        isolated_areas += tuple(assert_multipolygon(escalators_areas.intersection(self.clear_geometry)))
        isolated_areas += tuple(assert_multipolygon(self.clear_geometry.difference(escalators_and_stairs)))

        for isolated_area in isolated_areas:
            mpl_clear = shapely_to_mpl(isolated_area.buffer(0.01, join_style=JOIN_STYLE.mitre))
            mpl_stairs = tuple((stair, angle) for stair, angle in self.mpl_stairs
                               if mpl_clear.intersects_path(stair, filled=True))
            escalators = tuple(escalator for escalator in self._built_escalators
                               if escalator.mpl_geom.intersects_path(mpl_clear.exterior, filled=True))
            area = GraphArea(self, mpl_clear, mpl_stairs, escalators)
            area.prepare_build()
            self.areas.append(area)

    def build_points(self):
        narrowed_geometry = self._built_geometry.buffer(-0.6, join_style=JOIN_STYLE.mitre)
        geometry = narrowed_geometry.buffer(0.31, join_style=JOIN_STYLE.mitre).intersection(self.clear_geometry)

        if geometry.is_empty:
            return

        # points with 60cm distance to borders
        polygons = assert_multipolygon(geometry)
        for polygon in polygons:
            self._add_ring(polygon.exterior, want_left=False)

            for interior in polygon.interiors:
                self._add_ring(interior, want_left=True)

        # now fill in missing doorways or similar
        accessible_clear_geometry = geometry.buffer(0.31, join_style=JOIN_STYLE.mitre)
        missing_geometry = self.clear_geometry.difference(accessible_clear_geometry)
        polygons = assert_multipolygon(missing_geometry)
        for polygon in polygons:
            overlaps = polygon.buffer(0.02).intersection(accessible_clear_geometry)
            if overlaps.is_empty:
                continue

            points = []

            # overlaps to non-missing areas
            overlaps = assert_multipolygon(overlaps)
            for overlap in overlaps:
                points += self.add_point(overlap.centroid.coords[0])

            points += self._add_ring(polygon.exterior, want_left=False)

            for interior in polygon.interiors:
                points += self._add_ring(interior, want_left=True)

        # points around steps
        self.add_points_on_rings(self._built_isolated_areas)

    def _add_ring(self, geom, want_left):
        """
        add the points of a ring, but only those that have a specific direction change.
        additionally removes unneeded points if the neighbors can be connected in self.clear_geometry
        :param geom: LinearRing
        :param want_left: True if the direction has to be left, False if it has to be right
        """
        coords = []
        skipped = False
        can_delete_last = False
        for coord, is_left in get_coords_angles(geom):
            if is_left != want_left:
                skipped = True
                continue

            if not skipped and can_delete_last and len(coords) >= 2:
                if LineString((coords[-2], coord)).within(self.clear_geometry):
                    coords[-1] = coord
                    continue

            coords.append(coord)
            can_delete_last = not skipped
            skipped = False

        if not skipped and can_delete_last and len(coords) >= 3:
            if LineString((coords[-2], coords[0])).within(self.clear_geometry):
                coords.pop()

        points = []
        for coord in coords:
            points += self.add_point(coord)

        return points

    def add_points_on_rings(self, areas):
        for polygon in areas:
            for ring in (polygon.exterior,) + tuple(polygon.interiors):
                for linestring in assert_multilinestring(ring.intersection(self.clear_geometry)):
                    coords = tuple(linestring.coords)
                    if len(coords) == 2:
                        path = Path(coords)
                        length = abs(np.linalg.norm(path.vertices[0] - path.vertices[1]))
                        for coord in tuple(path.interpolated(int(length / 1.0 + 1)).vertices):
                            self.add_point(coord)
                        continue

                    start = 0
                    for segment in zip(coords[:-1], coords[1:]):
                        path = Path(segment)
                        length = abs(np.linalg.norm(path.vertices[0] - path.vertices[1]))
                        if length < 1.0:
                            coords = (path.vertices[1 if start == 0 else 0],)
                        else:
                            coords = tuple(path.interpolated(int(length / 1.0 + 0.5)).vertices)[start:]
                        for coord in coords:
                            self.add_point(coord)
                        start = 1

    def add_point(self, coord):
        if not self.mpl_clear.contains_point(coord):
            return []
        point = GraphPoint(coord[0], coord[1], self)
        self._built_points.append(point)
        for area in self.areas:
            area.add_point(point)
        return [point]

    def build_connections(self):
        if self._built_is_elevatorlevel:
            return

        for area in self.areas:
            area.build_connections()

    def connection_count(self):
        return np.count_nonzero(self.distances >= 0)

    def finish_build(self):
        self.areas = tuple(self.areas)
        self.points = tuple(point.i for point in self._built_points)

        set_points = set(self.points)
        if len(self.points) != len(set_points):
            print('ERROR: POINTS DOUBLE-ADDED (ROOM)', len(self.points), len(set_points))

        self.room_transfer_points = tuple(i for i in self.points if i in self.level.room_transfer_points)
        self.excludables = tuple(self.excludables)

        excludable_points = list()
        for excludable in self.excludables:
            points = self.level.arealocation_points[excludable]
            excludable_points.append(np.array(tuple((i in points) for i in self.points)))
        self.excludable_points = np.array(excludable_points)

        mapping = {point.i: i for i, point in enumerate(self._built_points)}

        empty = np.empty(shape=(len(self._built_points), len(self._built_points)), dtype=np.float16)
        empty[:] = np.inf

        ctypes = []
        distances = {}
        for from_point in self._built_points:
            for to_point, connection in from_point.connections.items():
                if to_point.i in mapping:
                    if connection.ctype not in distances:
                        ctypes.append(connection.ctype)
                        distances[connection.ctype] = empty.copy()
                    distances[connection.ctype][mapping[from_point.i], mapping[to_point.i]] = connection.distance

        self.ctypes = tuple(ctypes)
        self.distances = np.array(tuple(distances[ctype] for ctype in ctypes))

        for area in self.areas:
            area.finish_build()

    # Routing
    router_cache = {}

    def build_router(self, allowed_ctypes, allow_nonpublic, avoid, include):
        ctypes = tuple(i for i, ctype in enumerate(self.ctypes) if ctype in allowed_ctypes)
        avoid = tuple(i for i, excludable in enumerate(self.excludables) if excludable in avoid)
        include = tuple(i for i, excludable in enumerate(self.excludables) if excludable in include)
        cache_key = ('c3nav__graph__roomrouter__%s__%s__%s__%d__%s__%s' %
                     (self.graph.mtime, self.i, ','.join(str(i) for i in ctypes),
                      allow_nonpublic, ','.join(str(i) for i in avoid), ','.join(str(i) for i in include)))

        roomrouter = self.router_cache.get(cache_key)
        if not roomrouter:
            roomrouter = self._build_router(ctypes, allow_nonpublic, avoid, include)
            self.router_cache[cache_key] = roomrouter
        return roomrouter

    def _build_router(self, ctypes, allow_nonpublic, avoid, include):
        ctype_factors = np.ones((len(self.ctypes), 1, 1))*1000
        ctype_factors[ctypes, :, :] = 1

        distances = np.amin(self.distances*ctype_factors, axis=0).astype(np.float32)
        factors = np.ones_like(distances, dtype=np.float16)

        if ':nonpublic' in self.excludables and ':nonpublic' not in include:
            points, = self.excludable_points[self.excludables.index(':nonpublic')].nonzero()
            factors[points[:, None], :] = 1000 if allow_nonpublic else np.inf
            factors[:, points] = 1000 if allow_nonpublic else np.inf

        if avoid:
            points, = self.excludable_points[avoid, :].any(axis=0).nonzero()
            factors[points[:, None], :] = np.maximum(factors[points[:, None], :], 1000)
            factors[:, points] = np.maximum(factors[:, points], 1000)

        if include:
            points, = self.excludable_points[include, :].any(axis=0).nonzero()
            factors[points[:, None], :] = 1
            factors[:, points] = 1

        g_sparse = csgraph_from_dense(distances*factors, null_value=np.inf)
        shortest_paths, predecessors = shortest_path(g_sparse, return_predecessors=True)
        return RoomRouter(shortest_paths, predecessors)

    def get_connection(self, from_i, to_i):
        stack = self.distances[:, from_i, to_i]
        min_i = stack.argmin()
        distance = stack[min_i]
        ctype = self.ctypes[min_i]
        return GraphConnection(self.graph.points[self.points[from_i]], self.graph.points[self.points[to_i]],
                               distance=distance, ctype=ctype)

    def contains_point(self, point):
        return self.mpl_clear.contains_point(point)

    def connected_points(self, point, mode):
        connections = {}
        for area in self.areas:
            if area.contains_point(point):
                connections.update(area.connected_points(point, mode))
        return connections

    def check_connection(self, from_point, to_point):
        from_point = np.array(from_point)
        to_point = np.array(to_point)
        for area in self.areas:
            if area.contains_point(from_point) and area.contains_point(to_point):
                there, back = area.check_connection(from_point, to_point)
                if there is not None:
                    return there
        return None


RoomRouter = namedtuple('RoomRouter', ('shortest_paths', 'predecessors', ))
