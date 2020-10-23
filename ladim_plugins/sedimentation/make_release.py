import pandas as pd

from ladim_plugins.sedimentation import sinkvel
from ..release import make_release as mkrl


def main(config, fname=None):
    # Check if input argument is yaml file
    try:
        with open(config, encoding='utf8') as config_file:
            import yaml
            config = yaml.safe_load(config_file)
    except TypeError:
        pass
    except OSError:
        pass

    if isinstance(config, dict):
        config = [config]

    config = [convert_single_conf(**c) for c in config]
    return pd.DataFrame(mkrl(config, fname))


def convert_single_conf(
    location=None, depth=0, start_time='2000-01-01', stop_time='2000-01-01',
    num_particles=0, group_id=0,
):
    # Handle default arguments
    if location is None:
        location = dict(lat=0., lon=0.)

    return dict(
        date=[start_time, stop_time],
        num=num_particles,
        depth=depth,
        location=[location['lon'], location['lat']],
        attrs=dict(group_id=group_id, sinkvel=sinkvel),
    )


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('Usage: make_release <config.yaml> <out.rls>')
    elif len(sys.argv) == 2:
        out = main(sys.argv[1])
        print(out)
    else:
        main(sys.argv[1], sys.argv[2])
