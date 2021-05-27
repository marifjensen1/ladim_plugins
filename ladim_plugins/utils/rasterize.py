import xarray as xr
import numpy as np
from ladim_plugins.release.makrel import degree_diff_to_metric
from contextlib import contextmanager
import logging


logger = logging.getLogger(__name__)


def ladim_raster(particle_dset, grid_dset, weights=(None,)):
    """
    Convert LADiM output data to raster format.

    :param particle_dset: Particle file from LADiM (file name or xarray dataset)

    :param grid_dset: NetCDF file containing bin centers (file name or xarray dataset).

        * Any coordinate variable in the grid file which match the name of a variable
          in the LADiM file is used as bin centers, in the order which they appear in the
          grid file.

        * Bin edges are assumed to be halfway between bin centers, if not specified
          otherwise by a `bounds` attribute.

        * If the dataset includes a CF-compliant grid mapping, the output dataset will
          include a variable `cell_area` containing the area of the grid cells.

        * If the dataset includes a CF-compliant grid mapping, and the LADiM file has
          lon/lat attributes, the particle coordinates will be transformed before mapping
          to the rasterized grid.

    :param weights: A tuple containing parameters to be summed up. If `None` is one of
        the parameters, a variable named `bincount` is created which contains the sum
        of particles within each cell.

    :return: An `xarray` dataset containing the rasterized data.
    """
    # Add edge info to grid dataset, if not present already
    grid_dset = add_edge_info(grid_dset)

    def get_edg(dset, vname):
        """Get bin edges of a 1D coordinate with contiguous cells"""
        bndname = dset[vname].attrs['bounds']
        return dset[bndname].values[:, 0].tolist() + [dset[bndname].values[-1, 1]]

    # Add areal info to grid dataset, if not present already
    grid_dset = add_area_info(grid_dset)

    # Convert projection of ladim dataset, if necessary
    particle_dset = change_ladim_crs(particle_dset, grid_dset)

    # Compute histogram data
    raster = from_particles(
        particles=particle_dset,
        bin_keys=[v for v in grid_dset.coords],
        bin_edges=[get_edg(grid_dset, v) for v in grid_dset.coords],
        vdims=weights,
    )

    # Merge histogram data and grid data
    new_raster = grid_dset.assign({v: raster.variables[v] for v in raster.data_vars})
    if 'time' in raster.coords:
        new_raster = new_raster.assign_coords(time=raster.coords['time'])
    _assign_georeference_to_data_vars(new_raster)

    # Copy attrs from ladim dataset
    for varname in set(new_raster.variables).intersection(particle_dset.variables):
        for k, v in particle_dset[varname].attrs.items():
            if k not in new_raster[varname].attrs:
                new_raster[varname].attrs[k] = v

    # CF conventions attribute
    new_raster.attrs['Conventions'] = "CF-1.8"

    return new_raster


def change_ladim_crs(ladim_dset, grid_dset):
    """Add crs coordinates to ladim dataset, taken from a grid dataset"""

    # Abort if there is no lat/lon coordinates in the ladim dataset
    if 'lat' not in ladim_dset.variables or 'lon' not in ladim_dset.variables:
        return ladim_dset

    from pyproj import Transformer

    crs_varname = _get_crs_varname(grid_dset)
    crs_xcoord = _get_crs_xcoord(grid_dset)
    crs_ycoord = _get_crs_ycoord(grid_dset)

    # Abort if there is no grid mapping
    if any(v is None for v in [crs_xcoord, crs_ycoord, crs_varname]):
        return ladim_dset

    target_crs = get_projection(grid_dset[crs_varname].attrs)
    transformer = Transformer.from_crs("epsg:4326", target_crs)
    x, y = transformer.transform(ladim_dset.lat.values, ladim_dset.lon.values)

    return ladim_dset.assign(**{
        crs_xcoord: xr.Variable(ladim_dset.lon.dims, x),
        crs_ycoord: xr.Variable(ladim_dset.lat.dims, y),
    })


def get_projection(grid_opts):
    from pyproj import CRS

    std_grid_opts = dict(
        false_easting=0,
        false_northing=0,
    )

    proj4str_dict = dict(
        latitude_longitude="+proj=latlon",
        polar_stereographic=(
            "+proj=stere +ellps=WGS84 "
            "+lat_0={latitude_of_projection_origin} "
            "+lat_ts={standard_parallel} "
            "+lon_0={straight_vertical_longitude_from_pole} "
            "+x_0={false_easting} "
            "+y_0={false_northing} "
        ),
        transverse_mercator=(
            "+proj=tmerc +ellps=WGS84 "
            "+lat_0={latitude_of_projection_origin} "
            "+lon_0={longitude_of_central_meridian} "
            "+k_0={scale_factor_at_central_meridian} "
            "+x_0={false_easting} "
            "+y_0={false_northing} "
        ),
        orthographic=(
            "+proj=ortho +ellps=WGS84 "
            "+lat_0={latitude_of_projection_origin} "
            "+lon_0={longitude_of_projection_origin} "
            "+x_0={false_easting} "
            "+y_0={false_northing} "
        ),
    )

    proj4str_template = proj4str_dict[grid_opts['grid_mapping_name']]
    proj4str = proj4str_template.format(**std_grid_opts, **grid_opts)
    return CRS.from_proj4(proj4str)

    pass


def add_area_info(dset):
    """Add cell area information to dataset coordinates, if not already present"""
    crs_varname = _get_crs_varname(dset)
    crs_xcoord = _get_crs_xcoord(dset)
    crs_ycoord = _get_crs_ycoord(dset)

    # Do nothing if crs info is unavailable
    if crs_varname is None:
        return dset

    # Do nothing if cell area exists
    stdnames = [dset[v].attrs.get('standard_name', '') for v in dset.variables]
    if "cell_area" in stdnames:
        return dset

    x_bounds = dset[dset[crs_xcoord].attrs['bounds']].values
    y_bounds = dset[dset[crs_ycoord].attrs['bounds']].values
    x_diff = np.diff(x_bounds)[np.newaxis, :, 0]
    y_diff = np.diff(y_bounds)[:, np.newaxis, 0]
    grdmap = dset[crs_varname].attrs['grid_mapping_name']
    metric_projections = [
        'polar_stereographic', 'stereographic', 'orthographic', 'mercator',
        'transverse_mercator', 'oblique_mercator',
    ]

    # Compute cell area, or raise error if unknown mapping
    if grdmap == 'latitude_longitude':
        lon_diff_m, lat_diff_m = degree_diff_to_metric(
            lon_diff=x_diff, lat_diff=y_diff,
            reference_latitude=y_bounds.mean(axis=-1)[:, np.newaxis],
        )
        cell_area_data = lon_diff_m * lat_diff_m

    elif grdmap in metric_projections:
        cell_area_data = x_diff * y_diff
    else:
        raise NotImplementedError(f'Unknown grid mapping: {grdmap}')

    cell_area = xr.Variable(
        dims=(crs_ycoord, crs_xcoord),
        data=cell_area_data,
        attrs=dict(
            long_name="area of grid cell",
            standard_name="cell_area",
            units='m2',
        ),
    )

    return dset.assign(cell_area=cell_area)


def add_edge_info(dset):
    """Add edge information to dataset coordinates, if not already present"""
    dset_new = dset.copy()
    coords_without_bounds = [v for v in dset.coords if 'bounds' not in dset[v].attrs]
    for var_name in coords_without_bounds:
        bounds_name = var_name + '_bounds'
        edges_values = _edges(dset[var_name].values)
        bounds_var = xr.Variable(
            dims=dset[var_name].dims + ('bounds_dim', ),
            data=np.stack([edges_values[:-1], edges_values[1:]], axis=-1),
        )

        dset_new[var_name].attrs['bounds'] = bounds_name
        dset_new[bounds_name] = bounds_var

    return dset_new


def _edges(a):
    mid = 0.5 * (a[:-1] + a[1:])
    return np.concatenate([mid[:1] - (a[1] - a[0]), mid, mid[-1:] + a[-1] - a[-2]])


def _assign_georeference_to_data_vars(dset):
    crs_varname = _get_crs_varname(dset)
    crs_xcoord = _get_crs_xcoord(dset)
    crs_ycoord = _get_crs_ycoord(dset)

    for v in dset.data_vars:
        if crs_xcoord in dset[v].coords and crs_ycoord in dset[v].coords:
            dset[v].attrs['grid_mapping'] = crs_varname


def _get_crs_varname(dset):
    for v in dset.variables:
        if 'grid_mapping_name' in dset[v].attrs:
            return v
    return None


def _get_crs_xcoord(dset):
    for v in dset.coords:
        if 'standard_name' not in dset[v].attrs:
            continue
        s = dset[v].attrs['standard_name']
        if s == 'projection_x_coordinate' or s == 'grid_longitude' or s == 'longitude':
            return v
    return None


def _get_crs_ycoord(dset):
    for v in dset.coords:
        if 'standard_name' not in dset[v].attrs:
            continue
        s = dset[v].attrs['standard_name']
        if s == 'projection_y_coordinate' or s == 'grid_latitude' or s == 'latitude':
            return v
    return None


def from_particles(particles, bin_keys, bin_edges, vdims=(None,),
                   timevar_name='time', countvar_name='particle_count',
                   time_idx=None):

    # Handle variations on call signature
    if isinstance(particles, str):
        with xr.open_dataset(particles) as dset:
            return from_particles(dset, bin_keys, bin_edges, vdims)

    if countvar_name in particles:
        count = particles.variables[countvar_name].values
        indptr_name = particles.variables[bin_keys[0]].dims[0]
        indptr = np.cumsum(np.concatenate(([0], count)))
        slicefn = lambda tidx: particles.isel(
            {indptr_name: slice(indptr[tidx], indptr[tidx + 1])})
        tvals = particles[timevar_name].values
    elif timevar_name in particles.dims:
        slicefn = lambda tidx: particles.isel({timevar_name: tidx})
        tvals = particles.variables[timevar_name].values
    else:
        slicefn = lambda tidx: particles
        tvals = None

    if time_idx is not None:
        slicefn_old = slicefn
        slicefn = lambda tidx: slicefn_old(time_idx)
        tvals = None

    return _from_particle(slicefn, tvals, bin_keys, bin_edges, vdims)


def _from_particle(slicefn, tvals, bin_keys, bin_edges, vdims):
    # Get the histogram for each time slot and property
    field_list = []
    for tidx in range(len(tvals) if tvals is not None else 1):
        dset = slicefn(tidx)
        coords = [dset[k].values for k in bin_keys]
        weights = [None if w is None else dset[w].values for w in vdims]
        vals = [np.histogramdd(coords, bin_edges, weights=w)[0] for w in weights]
        field_list.append(vals)

    # Collect each histogram into an xarray variable
    field = np.array(field_list)
    xvars = {}
    for i, vdim in enumerate(vdims):
        vdim_name = 'bincount' if vdim is None else vdim
        kdims = ('time', ) + tuple(bin_keys)
        xvars[vdim_name] = xr.Variable(kdims, field[:, i])

    # Define bin center coordinates
    xcoords = {kdim: 0.5 * np.add(bin_edges[i][:-1], bin_edges[i][1:])
               for i, kdim in enumerate(bin_keys)}

    # Construct dataset
    if tvals is None:
        dset = xr.Dataset(xvars, xcoords).isel(time=0)
    else:
        xcoords['time'] = tvals
        dset = xr.Dataset(xvars, xcoords)

    return dset


@contextmanager
def load_mfladim(files):
    """Load multipart ladim files as xarray dataset"""

    import glob
    file_names = sorted(glob.glob(files))

    # --- Do single-file opening if only one file ---

    if len(file_names) == 1:
        yield xr.open_dataset(file_names[0])
        return

    # --- Check that the files follow a continuous, sequential ordering ---

    with xr.open_dataset(file_names[0]) as dset:
        instance_offset = dset.instance_offset.values.item()

    for fname in file_names:
        with xr.open_dataset(fname) as dset:
            if dset.instance_offset.values.item() != instance_offset:
                raise ValueError(
                    f'In {fname}: Expected instance_offset == {instance_offset}, '
                    f'found {dset.instance_offset.values.item()}'
                )
            instance_offset += len(dset.particle_instance)

    # --- Open and merge data files. Close them afterwards. ---

    dataset_particle_instance = None
    dataset_time = None
    dataset_first = None
    dataset_merged = None

    try:
        dataset_first = xr.open_dataset(file_names[0])
        dataset_particle = dataset_first.drop_dims(['particle_instance', 'time'])

        dataset_particle_instance = xr.open_mfdataset(
            paths=file_names,
            concat_dim='particle_instance',
            combine='nested',
            preprocess=lambda d: d.drop_dims(['particle', 'time']),
        )

        dataset_time = xr.open_mfdataset(
            paths=file_names,
            concat_dim='time',
            combine='nested',
            preprocess=lambda d: d.drop_dims(['particle', 'particle_instance']),
        )

        dataset_merged = xr.merge([
            dataset_particle,
            dataset_particle_instance.drop_vars('instance_offset'),
            dataset_time.drop_vars('instance_offset')
        ])

        yield dataset_merged

    finally:
        if dataset_merged:
            dataset_merged.close()
        if dataset_first:
            dataset_first.close()
        if dataset_particle_instance:
            dataset_particle_instance.close()
        if dataset_time:
            dataset_time.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='Convert LADiM output data to netCDF raster format.',
    )

    parser.add_argument("ladim_file", help="output file from LADiM")
    parser.add_argument(
        "grid_file",
        help="netCDF file containing the bins. Any coordinate variable in the file "
             "which match the name of a LADiM variable is used.")

    parser.add_argument(
        "raster_file",
        help="output file name",
    )

    parser.add_argument(
        "--weights",
        nargs='+',
        metavar='varname',
        help="weighting variables",
        default=(),
    )

    args = parser.parse_args()
    weights = (None, ) + tuple(args.weights)

    with xr.open_dataset(args.grid_file) as grid_dset:
        with load_mfladim(args.ladim_file) as ladim_dset:
            raster = ladim_raster(ladim_dset, grid_dset, weights=weights)

    raster.to_netcdf(args.raster_file)


if __name__ == '__main__':
    main()
