'''
    distribute regional statistic by land use and area
    inputs:
        region shapefile with metrics
        land-use raster
        scaling factors

        given a raster cell, find all polygons that overlap (spatial index?)
        get the percentage coverage of each land use type
        based on inputed parameters
'''


import rasterio
import fiona
import numpy as np
from collections import OrderedDict
from src import util
from os import path
import subprocess

def distribute_region_statistics(region_shapefile, land_use_raster_file, region_raster_file, output_folder, land_use_categories):
    with rasterio.open(land_use_raster_file, 'r') as land_use_raster:
        with rasterio.open(region_raster_file, 'r') as region_raster:
                region_stats = build_region_stats_lookup_table(region_shapefile)

                resolution = 100
                #trim land
                affine_LU = land_use_raster.profile['affine']
                affine_regions = region_raster.profile['affine']

                lu_array = land_use_raster.read()

                #move the bands to the third dimension to make calculations easier to work with
                #lu_array is the 100m raster of land use separated by bands. must be x100 to get land use in area.
                #need to make sure we move up from ubyte to uint32 to store larger values that we will sue
                cell_land_use_m2 = np.rollaxis(lu_array, 0, 3).astype(np.uint32) * resolution
                print cell_land_use_m2.shape, cell_land_use_m2.dtype



                #remove remaining column
        #        REMAINDER_COL = 0
        #        cell_land_use_m2 = np.delete(cell_land_use_m2,REMAINDER_COL, axis=2)
                #repeat summed area value so that the divison against each band will work
                #cell_land_use_m2 = np.true_divide(
                #       cell_land_use_m2,
                #        np.atleast_3d(np.sum(cell_land_use_m2, axis=2))
                #)
                #cell_land_use_m2 = np.nan_to_num(cell_land_use_m2)

                print cell_land_use_m2[13:20,790:800,0]

                (region_code_array,) = region_raster.read()
                print "region code array shape: ", region_code_array.shape
                print "region height:", region_raster.profile['height']

                region_land_use_split_m2 = np.zeros(cell_land_use_m2.shape)
                region_population = np.zeros(cell_land_use_m2.shape)
                region_employment = np.zeros(cell_land_use_m2.shape)
                print "region_land_use_split_m2:", region_land_use_split_m2.shape, region_land_use_split_m2.dtype
                print "region_population:", region_population.shape, region_population.dtype

                #for each land use band
                for (row,col,z), v in np.ndenumerate(cell_land_use_m2):
                    #if not math.isnan(v):
                        #TODO: auto clip land-use raster to region raster
                        #location region_id from region_raster
                        try:
                            region_id = int(region_code_array[row,col])
                        except IndexError:
                            region_id = None

                        #pull the regional land use percentage for each region
                        if region_id and region_id in region_stats:
                            land_use_type = land_use_categories[z+1] #row 0 is now land use type 1
                            landuse_area = region_stats[region_id][land_use_type]

                        #    if row in xrange(13,30) and col in xrange(790,800):
                        #        print (row, col), z, int(region_id), pc_landuse, area_landuse, "=", value

                            region_land_use_split_m2[row, col, z] = landuse_area
                            #assign population value
                            try:
                                region_population[row, col] = region_stats[region_id]['pop_2008']
                                region_employment[row, col] = region_stats[region_id]['emp_2008']
                            except KeyError:
                                print region_id, region_stats[region_id]['GEN'], "not in pop/emp dataset"
                                region_population[row, col] = 0
                                region_employment[row, col] = 0

                print region_land_use_split_m2[13:20,790:800,0]

                #for k in region_stats.keys():
                #    print region_stats[k]["Area_cover"], region_stats[k]["cell_area"], region_stats[k]["Area_cover"] - region_stats[k]["cell_area"]

                with np.errstate(divide='ignore', invalid='ignore'):
                    cell_lu_pc = np.true_divide(cell_land_use_m2,region_land_use_split_m2)
                    cell_lu_pc[cell_lu_pc == np.inf] = 0
                    cell_lu_pc = np.nan_to_num(cell_lu_pc)

                scale_factors = [0.7,0.05,0,0.01,0]

                pop_scaling_vector = np.array(scale_factors) / sum(scale_factors)
                emp_scaling_vector = np.array(scale_factors) / sum(scale_factors)

                pop_result = scale_stat_array_by_land_use(region_population, cell_lu_pc, pop_scaling_vector)
                emp_result = scale_stat_array_by_land_use(region_employment, cell_lu_pc, emp_scaling_vector)

                #for (row,col), v in np.ndenumerate(result):
                #    if v > 0:
                #        print (row, col), v

                population_output_file = path.join(output_folder, "population_{resolution}m.tif"
                                                   .format(resolution = resolution))
                employment_output_file = path.join(output_folder, "employment_{resolution}m.tif"
                                                   .format(resolution = resolution))

                profile = region_raster.profile
                profile.update(dtype=rasterio.uint32)
                #profile.update(dtype=rasterio.ubyte)
                print "Writing population to: ", population_output_file
                print profile
                with rasterio.open(population_output_file, 'w', **profile) as out:
                    out.write(pop_result, indexes=1)

                print "Writing employment to: ", employment_output_file
                with rasterio.open(employment_output_file, 'w', **profile) as out:
                    out.write(emp_result, indexes=1)

def scale_stat_array_by_land_use(a, cell_lu_area_pc, scaling_vector):
    print "calculating population raster with scale vector:", scaling_vector
    a_land_use_distributed = np.atleast_3d(a) * scaling_vector

    a_by_lu = cell_lu_area_pc * a_land_use_distributed

    summed_a_by_lu = np.sum(a_by_lu, axis=2)

    result = np.ceil(summed_a_by_lu).astype(np.uint32)

    return result

def build_region_stats_lookup_table(region_shapefile):

    with fiona.open(region_shapefile, 'r') as region_features:

        #print list(region['properties'])[0], list(region['properties'])[1:]
        #print region_features[0]['properties']
        region_attrs = [region['properties'].items() for region in region_features]
        #print region_attrs
        stat_dict = {attrs[0][1] : OrderedDict(attrs[1:]) for attrs in region_attrs}

        #print stat_dict
    return stat_dict


import rasterstats
from math import sqrt, pow
def check_raster_output(region_shapefile, stats_raster, fields):
    zs = rasterstats.zonal_stats(region_shapefile, stats_raster, stats=['sum'])
    with fiona.open(region_shapefile) as regions:
        results = []
        for (region, stat) in zip(regions, zs):
            try:
                actual=sum([float(region['properties'][f]) for f in fields])
                calculated = (float(stat['sum']))
                results.append((actual, calculated))
            except TypeError:
                #print "no value for ", region['properties']['AGS_Int']
                results.append((0,0))


    print "results for", stats_raster,"..."
    print results
    actuals, calcd = zip(*results)
    print "\t actual:", sum(actuals)
    print "\t calculated:", sum(calcd)
    print "\t difference:", sum(actuals) - sum(calcd)
    print "\t RMSE:", sqrt(sum([pow(a-b,2) for (a,b) in results]) / len(results))


def add_rasters(a_file,b_file, outputfile):
    with rasterio.open(a_file) as a:
        with rasterio.open(b_file) as b:
            profile = a.profile
            with rasterio.open(outputfile, 'w', **profile) as out:
                c = a.read(1) + b.read(1)
                out.write(c, indexes=1)
  #  cmd = ["gdal_calc.py",
  ##         "-A", a_file,
  #         "-B", b_file,
  #         "--outfile={outfile}".format(outfile = outputfile),
  #         '--calc="A+B"']

#    print cmd
 #   subprocess.check_call(cmd)


if __name__ == "__main__":

    land_use_categories = util.load_land_use_mapping(1)
    RUN_DISTRIBUTE = False
    RUN_CHECKING = True

    if RUN_DISTRIBUTE:
        distribute_region_statistics(
                                 "../../output/regions_with_land_use",
                                "../../data/land_use/land_use_100m_clipped.tif",
                                 "../../data/regional/region_raster.tif",
                                 "../../output/regions_with_land_use",
                                 "../../output/population_100m.tif",
                                 land_use_categories)

    if RUN_CHECKING:
        #check_raster_output("../../data/regional/regions_lu_pop_emp.geojson", "../../output/population_100m.tif")
        #check_raster_output("../../data/temp/regions_with_land_use", "../../output/population_100m.tif")
        check_raster_output("../../data/temp/regions_with_land_use", "../../data/test_output/population_100m.tif")

