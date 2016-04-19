import numpy
import os
from osgeo import ogr, osr
import psycopg2
from octtree import OcttreeLeaf, OcttreeNode
import octtree
from rasterstats import zonal_stats
import affine

def load_regions(shapefile, baseSpatialRef):
    polygons = []
    driver = ogr.GetDriverByName("ESRI Shapefile")
    # load shapefile
    dataSource = driver.Open(shapefile, 0)

    layer = load_layer_from_shapefile(dataSource)
    for feature in layer:
        geom = feature.GetGeometryRef().Clone()

        #convert to EPSG:3035
        outSpatialRef = osr.SpatialReference()
        outSpatialRef.ImportFromEPSG(baseSpatialRef)
        geom.TransformTo(outSpatialRef)

        if geom.GetGeometryName() in ['MULTIPOLYGON', 'GEOMETRYCOLLECTION'] :
            for geom_part in geom:
                if geom_part.GetGeometryName() == 'POLYGON':
                    polygons.append(geom_part.Clone())
        elif geom.GetGeometryName() == 'POLYGON':
            polygons.append(geom)

    return polygons

def load_layer_from_shapefile(dataSource):

    if dataSource is None:
        print 'Could not open %s' % (dataSource.GetName())
        return None
    else:
        print 'Opened %s' % (dataSource.GetName())
        layer = dataSource.GetLayer()

        featureCount = layer.GetFeatureCount()
        fieldCount = layer.GetLayerDefn().GetFieldCount()
        print "Number of features: %d, Number of fields: %d" % (featureCount, fieldCount)
        return layer

def build_region_octtree(regions):
    boundary = merge_polygons(regions)
    ot = None
    children = [OcttreeNode(region,[], ot) for region in regions]
    ot = OcttreeNode(boundary, children, None)
    return ot

#Round up to next higher power of 2 (return x if it's already a power of 2).
#from http://stackoverflow.com/questions/1322510
def next_power_of_2(n):
    """
    Return next power of 2 greater than or equal to n
    """
    return 2**(n-1).bit_length()

def solve_iteratively(Config, region_octtree, regions, pop_array, affine, boundary):
    ##
    # if num zones is too large, we need a higher threshold
    # keep a record of the thresholds that result in the nearest low, and nearest high
    # for the next step, take the halfway number between the two

    desired_num_zones = Config.getint("Parameters", "population_threshold")
    best_low = Config.getint("Parameters", "lower_population_threshold")
    best_high = Config.getint("Parameters", "upper_population_threshold")
    tolerance =  Config.getfloat("Parameters", "tolerance")

    step = 1
    solved = False
    num_zones = 0
    #TODO: flag to choose whether to include empty zones in counting, and when saving?

    pop_threshold = (best_high - best_low) / 2


    while not solved: # difference greater than 10%
        print 'step %d with threshold level %d...' % (step, pop_threshold)
        region_octtree = octtree.build_out_nodes(Config, region_octtree, regions, pop_array, affine, pop_threshold)
        num_zones = region_octtree.count_populated()
        print "\tnumber of cells:", num_zones
        print ''

        solved = abs(num_zones - desired_num_zones)/float(desired_num_zones) < tolerance
        if not solved:
            if num_zones > desired_num_zones:
                best_low = max (best_low, pop_threshold)
            else:
                best_high = min (best_high, pop_threshold)
            pop_threshold = (best_low + best_high) / 2

        step += 1

    print "Solution found!"
    print "\t%6d zones" % (num_zones)
    print "\t%6d threshold" % (pop_threshold)

    return region_octtree


def load_data(Config, array_origin_x, array_origin_y, size, inverted=False):

    database_string = Config.get("Input", "databaseString")
    if database_string:
        conn = psycopg2.connect(None, "arcgis", "postgres", "postgres")
    else:
        db = Config.get("Input", "database")
        user = Config.get("Input", "user")
        pw = Config.get("Input", "password")
        host = Config.get("Input", "host")

        conn = psycopg2.connect(database=db, user=user, password=pw, host=host)
    cursor = conn.cursor()

    sql = Config.get("Input", "sql")

    resolution = Config.getint("Input", "resolution")

    x_max = array_origin_x + size * resolution
    y_max = array_origin_y + size * resolution

    pop_array = numpy.zeros((size, size), dtype=numpy.int32)

    #cursor.execute("SELECT x_mp_100m, y_mp_100m, \"Einwohner\" FROM public.muc_all_population;")
    #this metheod only works when total rows = ncols x nrows in database. (IE no missing values)
    print "parameters", (array_origin_x, x_max, array_origin_y, y_max)
    cursor.execute(sql, (array_origin_x, x_max, array_origin_y, y_max)) #xmin xmax, ymin, ymax in that order
    #ttes charhra

    a = affine.Affine(100,0,array_origin_x,0,-100,array_origin_y+size*resolution)

    for line in cursor:
        if line[2] > 0:
            (x,y) = (line[0], line[1])
            (col, row) = ~a * (x,y)
            pop_array[row, col] = line[2]
        #reference arrays by (row_no , col_no)
        #reference arrays by (   a_y,      a_x   )

    print numpy.sum(pop_array)

    return (pop_array, a)

def tabulate_intersection(zone_octtree, octtreeSaptialRef, shapefile, inSpatialEPSGRef, class_field):
    print "running intersection tabulation"
    driver = ogr.GetDriverByName("ESRI Shapefile")
    # load shapefile
    dataSource = driver.Open(shapefile, 0)
    layer = load_layer_from_shapefile(dataSource)

    #get all distinct class_field values
    features = [feature.Clone() for feature in layer]

    field_values = list({f.GetField(class_field)[:8] for f in features})

    #set value for each zones and class to zero
    zones = {node: {} for node in zone_octtree.iterate()}

    for z in zones.iterkeys():
        for c in field_values:
            zones[z][c] = 0

    for feature in features:
        poly_class = feature.GetField(class_field)[:8]
        poly = feature.GetGeometryRef().Clone()

        inSpatialRef = osr.SpatialReference()
        inSpatialRef.ImportFromEPSG(inSpatialEPSGRef) #Germany zone 4 for ALKIS data

        outSpatialRef = osr.SpatialReference()
        outSpatialRef.ImportFromEPSG(octtreeSaptialRef)
        transform = osr.CoordinateTransformation(inSpatialRef, outSpatialRef)
        poly.Transform(transform)

        matches = zone_octtree.find_matches(poly, poly_class)

        for (zone, (class_name, percentage)) in matches:
            #print zone.index, class_name, percentage
            zones[zone][class_name] += percentage

    return (field_values, zones)

def save_with_landuse(filename, outputSpatialReference, field_values, intersections):
    print "saving zones with land use to:", filename
    driver = ogr.GetDriverByName("ESRI Shapefile")
    # create the data source
    if os.path.exists(filename):
        driver.DeleteDataSource(filename)
    data_source = driver.CreateDataSource(filename)

    outputSRS = osr.SpatialReference()
    outputSRS.ImportFromEPSG(outputSpatialReference)

    layer = data_source.CreateLayer("zones", outputSRS, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("fid", ogr.OFTInteger))
    layer.CreateField(ogr.FieldDefn("Population", ogr.OFTInteger))

    for f in field_values:
        layer.CreateField(ogr.FieldDefn(f, ogr.OFTReal))

    for zone, classes in intersections.iteritems():
        feature = zone.to_feature(layer)
        for c, percentage in classes.iteritems():
            feature.SetField(c, percentage)
        if feature.GetGeometryRef().GetGeometryType() == 3: #is a polygon
            layer.CreateFeature(feature)

        feature.Destroy()


    data_source.Destroy()

def save_tree_only(filename, outputSpatialReference, octtree):
    print "saving zones to:", filename
    driver = ogr.GetDriverByName("ESRI Shapefile")
    # create the data source
    if os.path.exists(filename):
        driver.DeleteDataSource(filename)
    data_source = driver.CreateDataSource(filename)

    outputSRS = osr.SpatialReference()
    outputSRS.ImportFromEPSG(outputSpatialReference)

    layer = data_source.CreateLayer("zones", outputSRS, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("fid", ogr.OFTInteger))
    layer.CreateField(ogr.FieldDefn("Population", ogr.OFTInteger))

    for node in octtree.iterate():
        feature = node.to_feature(layer)

        layer.CreateFeature(feature)

        feature.Destroy()


    data_source.Destroy()

def quarter_polygon(geom_poly):
    #https://pcjericks.github.io/py-gdalogr-cookbook/geometry.html#quarter-polygon-and-create-centroids
    geom_poly_envelope = geom_poly.GetEnvelope()
    minX = geom_poly_envelope[0]
    minY = geom_poly_envelope[2]
    maxX = geom_poly_envelope[1]
    maxY = geom_poly_envelope[3]

    '''
    coord0----coord1----coord2
    |           |           |
    coord3----coord4----coord5
    |           |           |
    coord6----coord7----coord8
    '''
    coord0 = minX, maxY
    coord1 = minX+(maxX-minX)/2, maxY
    coord2 = maxX, maxY
    coord3 = minX, minY+(maxY-minY)/2
    coord4 = minX+(maxX-minX)/2, minY+(maxY-minY)/2
    coord5 = maxX, minY+(maxY-minY)/2
    coord6 = minX, minY
    coord7 = minX+(maxX-minX)/2, minY
    coord8 = maxX, minY

    ringTopLeft = ogr.Geometry(ogr.wkbLinearRing)
    ringTopLeft.AddPoint_2D(*coord0)
    ringTopLeft.AddPoint_2D(*coord1)
    ringTopLeft.AddPoint_2D(*coord4)
    ringTopLeft.AddPoint_2D(*coord3)
    ringTopLeft.AddPoint_2D(*coord0)
    polyTopLeft = ogr.Geometry(ogr.wkbPolygon)
    polyTopLeft.AddGeometry(ringTopLeft)


    ringTopRight = ogr.Geometry(ogr.wkbLinearRing)
    ringTopRight.AddPoint_2D(*coord1)
    ringTopRight.AddPoint_2D(*coord2)
    ringTopRight.AddPoint_2D(*coord5)
    ringTopRight.AddPoint_2D(*coord4)
    ringTopRight.AddPoint_2D(*coord1)
    polyTopRight = ogr.Geometry(ogr.wkbPolygon)
    polyTopRight.AddGeometry(ringTopRight)


    ringBottomLeft = ogr.Geometry(ogr.wkbLinearRing)
    ringBottomLeft.AddPoint_2D(*coord3)
    ringBottomLeft.AddPoint_2D(*coord4)
    ringBottomLeft.AddPoint_2D(*coord7)
    ringBottomLeft.AddPoint_2D(*coord6)
    ringBottomLeft.AddPoint_2D(*coord3)
    polyBottomLeft = ogr.Geometry(ogr.wkbPolygon)
    polyBottomLeft.AddGeometry(ringBottomLeft)


    ringBottomRight = ogr.Geometry(ogr.wkbLinearRing)
    ringBottomRight.AddPoint_2D(*coord4)
    ringBottomRight.AddPoint_2D(*coord5)
    ringBottomRight.AddPoint_2D(*coord8)
    ringBottomRight.AddPoint_2D(*coord7)
    ringBottomRight.AddPoint_2D(*coord4)
    polyBottomRight = ogr.Geometry(ogr.wkbPolygon)
    polyBottomRight.AddGeometry(ringBottomRight)

    quaterPolyTopLeft = polyTopLeft.Intersection(geom_poly)
    quaterPolyTopRight =  polyTopRight.Intersection(geom_poly)
    quaterPolyBottomLeft =  polyBottomLeft.Intersection(geom_poly)
    quaterPolyBottomRight =  polyBottomRight.Intersection(geom_poly)

    multipolys = [quaterPolyTopLeft, quaterPolyTopRight, quaterPolyBottomLeft, quaterPolyBottomRight]
    polys = []

    for geom in multipolys:
        if geom.GetGeometryName() in ['MULTIPOLYGON', 'GEOMETRYCOLLECTION'] :
            for geom_part in geom:
                if geom_part.GetGeometryName() == 'POLYGON':
                    polys.append(geom_part.Clone())
        else:
            polys.append(geom)


    return polys

def get_geom_parts(geom):
    parts = []
    if geom.GetGeometryName() in ['MULTIPOLYGON', 'GEOMETRYCOLLECTION'] :
        for geom_part in geom:
            if geom_part.GetGeometryName() == 'POLYGON':
                parts.append(geom_part.Clone())
    elif geom.GetGeometryName() == 'POLYGON': #ignore linestrings and multilinestrings
        parts.append(geom)
    return parts


def calculate_pop_value(node, array, transform):
    stats = zonal_stats(node.polygon.ExportToWkb(), array, affine=transform, stats="sum", nodata=-1)
    total = stats[0]['sum']
    if total:
        return total
    else:
        return 0

def merge_polygons(polygons):
    unionc = ogr.Geometry(ogr.wkbMultiPolygon)
    for p in polygons:
        unionc.AddGeometry(p)
    union = unionc.UnionCascaded()
    return union

def find_best_neighbour(node, neighbours, vert_shared, hori_shared):
    max_length = 0
    best_neighbour = None
    for neighbour in neighbours:
        if node.index != neighbour.index and node.polygon.Touches(neighbour.polygon):
            #neighbour_area = neighbour.polygon.GetArea()
            length = get_common_edge_length(node, neighbour, vert_shared, hori_shared)
            if length > max_length:
                max_length = length
                best_neighbour = neighbour
    if max_length == 0:
        print "failed for node:", node.index, "against ", [n.index for n in neighbours]

    return best_neighbour

import shapely
from shapely.geometry import LineString

def get_common_edge_length(node1, node2, geom_vertical_line_parts_map, geom_horizontal_line_parts_map):
    edge_length = 0

    if node1 not in geom_vertical_line_parts_map:
        print "missing node1:", node1.index
    if node2 not in geom_vertical_line_parts_map:
        print "missing node2:", node2.index

    #get all intersecting lines
    vert_shared = [h1.intersection(h2)
                   for h1 in geom_vertical_line_parts_map[node1]
                   for h2 in geom_vertical_line_parts_map[node2]]

    hori_shared = [h1.intersection(h2)
                   for h1 in geom_horizontal_line_parts_map[node1]
                   for h2 in geom_horizontal_line_parts_map[node2]]

    for l in hori_shared+vert_shared:
        if l.geometryType() == "LineString":
            print l.geometryType(), l, l.length
            edge_length = edge_length + l.length

    return edge_length

def get_common_boundary(node1, node2):
    geom1 = shapely.wkb.loads(node1.polygon.ExportToWkb())
    geom2 = shapely.wkb.loads(node2.polygon.ExportToWkb())

    lines1 = zip(geom1.exterior.coords[0:-1],geom1.exterior.coords[1:])
    lines2 = zip(geom2.exterior.coords[0:-1],geom2.exterior.coords[1:])

    vert1 = [LineString([(ax,ay),(bx,by)]) for (ax,ay),(bx,by) in lines1 if ax == bx]
    hori1 = [LineString([(ax,ay),(bx,by)]) for (ax,ay),(bx,by) in lines1 if ay == by]

    vert2 = [LineString([(ax,ay),(bx,by)]) for (ax,ay),(bx,by) in lines2 if ax == bx]
    hori2 = [LineString([(ax,ay),(bx,by)]) for (ax,ay),(bx,by) in lines2 if ay == by]

    edge_length = 0

    #get all intersecting lines
    vert_shared = [h1.intersection(h2)
                   for h1 in vert1
                   for h2 in vert2]

    hori_shared = [h1.intersection(h2)
                   for h1 in hori1
                   for h2 in hori2]

    for l in hori_shared+vert_shared:
        if l.geometryType() == "LineString":
            #print l.geometryType(), l, l.length
            edge_length = edge_length + l.length

    return edge_length


def find_best_neighbour(node, neighbours):
    max_length = 0
    best_neighbour = None
    for neighbour in neighbours:
        if node.index != neighbour.index and node.polygon.Touches(neighbour.polygon):
            #neighbour_area = neighbour.polygon.GetArea()
            length = get_common_boundary(node, neighbour)
            if length > max_length:
                max_length = length
                best_neighbour = neighbour
    if max_length == 0:
        print "failed for node:", node.index, "against ", [n.index for n in neighbours]

    return best_neighbour
