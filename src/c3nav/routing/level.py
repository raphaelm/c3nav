import os

from django.conf import settings
from PIL import Image, ImageDraw
from shapely.geometry import JOIN_STYLE

from c3nav.mapdata.utils import assert_multipolygon
from c3nav.routing.point import GraphPoint
from c3nav.routing.room import GraphRoom
from c3nav.routing.utils.base import get_nearest_point
from c3nav.routing.utils.draw import _ellipse_bbox, _line_coords


class GraphLevel():
    def __init__(self, graph, level):
        self.graph = graph
        self.level = level
        self.points = []
        self.no_room_points = []
        self.rooms = []

    def build(self):
        print('Level %s:' % self.level.name)
        self.collect_rooms()
        print('%d rooms' % len(self.rooms))

        for room in self.rooms:
            room.create_points()

        self.create_doors()
        self.create_levelconnectors()

        for room in self.rooms:
            room.connect_points()

        print('%d points' % len(self.points))
        print('%d room transfer points' % len(self.no_room_points))
        print()

    def collect_rooms(self):
        accessibles = self.level.geometries.accessible
        accessibles = assert_multipolygon(accessibles)
        for geometry in accessibles:
            GraphRoom(self, geometry)

    def create_doors(self):
        doors = self.level.geometries.doors
        doors = assert_multipolygon(doors)
        for door in doors:
            polygon = door.buffer(0.01, join_style=JOIN_STYLE.mitre)
            center = door.centroid
            center_point = GraphPoint(center.x, center.y, level=self)

            num_points = 0
            for room in self.rooms:
                if not polygon.intersects(room.geometry):
                    continue

                for subpolygon in assert_multipolygon(polygon.intersection(room.geometry)):
                    nearest_point = get_nearest_point(room.clear_geometry, subpolygon.centroid)
                    point = GraphPoint(nearest_point.x, nearest_point.y, room)
                    center_point.connect_to(point)
                    point.connect_to(center_point)
                    num_points += 1

            if num_points < 2:
                print('door with <2 num_points (%d) detected at (%.2f, %.2f)' % (num_points, center.x, center.y))

    def create_levelconnectors(self):
        for levelconnector in self.level.levelconnectors.all():
            polygon = levelconnector.geometry

            for room in self.rooms:
                if not polygon.intersects(room.geometry):
                    continue

                for subpolygon in assert_multipolygon(polygon.intersection(room.geometry)):
                    point = subpolygon.centroid
                    if not point.within(room.clear_geometry):
                        point = get_nearest_point(room.clear_geometry, point)
                    point = GraphPoint(point.x, point.y, room)
                    self.graph.add_levelconnector_point(levelconnector, point)

    def draw_png(self, points=True, lines=True):
        filename = os.path.join(settings.RENDER_ROOT, 'level-%s.base.png' % self.level.name)
        graph_filename = os.path.join(settings.RENDER_ROOT, 'level-%s.graph.png' % self.level.name)

        im = Image.open(filename)
        height = im.size[1]
        draw = ImageDraw.Draw(im)
        if lines:
            for point in self.points:
                for otherpoint, connection in point.connections.items():
                    draw.line(_line_coords(point, otherpoint, height), fill=(255, 100, 100))

        if points:
            for point in self.points:
                draw.ellipse(_ellipse_bbox(point.x, point.y, height), (200, 0, 0))

        for point in self.no_room_points:
            draw.ellipse(_ellipse_bbox(point.x, point.y, height), (0, 0, 255))

        for point in self.points:
            for otherpoint, connection in point.connections.items():
                if otherpoint in self.graph.no_level_points:
                    draw.line(_line_coords(point, otherpoint, height), fill=(0, 255, 255))

        im.save(graph_filename)