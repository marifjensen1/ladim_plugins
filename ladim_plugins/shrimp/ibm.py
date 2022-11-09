import numpy as np


class IBM:

    def __init__(self, config):
        # One entry for each pelagic stage
        self.vertmix = np.array([0.01, 0.01])
        self.depth_day = np.array([20, 20])
        self.depth_ngh = np.array([0, 0])
        self.speed = np.array([0.001, 0.002])

        self.grid = None
        self.state = None
        self.forcing = None
        self.dt = config['dt']

    def update_ibm(self, grid, state, forcing):
        self.grid = grid
        self.state = state
        self.forcing = forcing

        self.growth()
        self.mixing()
        self.diel_migration()

    def growth(self):
        # Reference paper: Ouellet and Chabot (2005), doi: 10.1007/s00227-005-1625-6

        alpha = 156.31578947368422
        beta = 23.315789473684212

        temp = self.state['temp']

        delta_age = self.dt / 86400
        delta_stage = 5 * delta_age * temp / (alpha + beta * temp)

        self.state['age'] += delta_age
        self.state['stage'] += delta_stage
        self.state['stage'] = np.minimum(6, self.state['stage'])  # 6 is the maximum stage
        self.state['active'] = self.state['stage'] < 6  # Stage 6 does not move with the currents

    def mixing(self):
        int_stage = np.minimum(5, np.int32(self.state['stage'])) - 1
        vertmix = self.vertmix[int_stage]

        z = self.state['Z']
        dw = np.random.normal(size=len(z))
        dz = np.sqrt(2 * vertmix * self.dt) * dw
        z += dz
        z[z < 0] *= -1  # Reflective boundary at surface
        self.state['Z'] = z

    def diel_migration(self):
        # Extract state parameters
        time = self.state['time']
        x = self.state['X']
        y = self.state['Y']
        z = self.state['Z']

        # Select parameters based on stage
        int_stage = np.minimum(5, np.int32(self.state['stage'])) - 1
        speed = self.speed[int_stage]
        depth_day = self.depth_day[int_stage]
        depth_ngh = self.depth_ngh[int_stage]

        # Find preferred depth
        lon, lat = self.grid.lonlat(x, y)
        is_day = sunheight(time, lon, lat) > 0
        preferred_depth = np.where(is_day, depth_day, depth_ngh)

        # Swim towards preferred depth
        speed_sign = np.zeros(len(z))  # Zero if within preferred range
        speed_sign[z > preferred_depth] = -1  # Upwards if too deep
        speed_sign[z < preferred_depth] = 1  # Downwards if too shallow
        z += self.dt * speed * speed_sign

        self.state['Z'] = z

    def old_stuff(self):
        # Init stuff
        stage_duration = np.array(config['ibm']['stage_duration'])  # [days]
        self.devel_rate = 1 / (stage_duration * 24 * 60 * 60)  # [s^-1]

        self.vertical_mixing = np.array(config['ibm']['vertical_mixing'])  # [m2/s]
        self.vertical_speed = np.array(config['ibm']['vertical_speed'])  # [m/s]

        self.maxdepth_day = np.array(config['ibm']['maxdepth_day'])  # [m]
        self.maxdepth_ngh = np.array(config['ibm']['maxdepth_night'])  # [m]
        self.mindepth_day = np.array(config['ibm']['mindepth_day'])  # [m]
        self.mindepth_ngh = np.array(config['ibm']['mindepth_night'])  # [m]

        # Select parameters based on stage
        int_stage = np.minimum(5, np.int32(state['stage'])) - 1
        devel_rate = self.devel_rate[int_stage]
        vertical_mixing = self.vertical_mixing[int_stage]
        vertical_speed = self.vertical_speed[int_stage]
        maxdepth_day = self.maxdepth_day[int_stage]
        maxdepth_ngh = self.maxdepth_ngh[int_stage]
        mindepth_day = self.mindepth_day[int_stage]
        mindepth_ngh = self.mindepth_ngh[int_stage]

        # --- Larval development ---
        state['age'] += self.dt
        state['stage'] += self.dt * devel_rate
        state['stage'] = np.minimum(state['stage'], 6)  # 6 is the maximum stage
        state['active'] = state['stage'] < 6  # Stage 6 does not move with the currents

        # --- Vertical random migration / turbulent mixing ---
        dw = np.random.normal(size=len(state.X))
        dz = np.sqrt(2 * vertical_mixing * self.dt) * dw
        state['Z'] += dz
        state['Z'][state['Z'] < 0] *= -1  # Reflective boundary at surface

        # --- Diel migration ---
        lon, lat = grid.lonlat(state.X, state.Y)
        is_day = sunheight(state.timestamp, lon, lat) > 0
        maxdepth = np.where(is_day, maxdepth_day, maxdepth_ngh)
        mindepth = np.where(is_day, mindepth_day, mindepth_ngh)

        vspeed_sign = np.zeros(len(state.X))  # Zero if within preferred range
        vspeed_sign[state.Z > maxdepth] = -1    # Upwards if too deep
        vspeed_sign[state.Z < mindepth] = 1     # Downwards if too shallow
        state['Z'] += self.dt * vertical_speed * vspeed_sign


def sunheight(time, lon, lat):
    RAD_PER_DEG = np.pi / 180.0
    DEG_PER_RAD = 180 / np.pi

    dtime = np.datetime64(time).astype(object)
    lon = np.array(lon)
    lat = np.array(lat)

    time_tuple = dtime.timetuple()
    # day of year, original does not consider leap years
    yday = time_tuple.tm_yday
    # hours in UTC (as output from oceanographic model)
    hours = time_tuple.tm_hour

    phi = lat * RAD_PER_DEG

    # Compute declineation = delta
    a0 = 0.3979
    a1 = 0.9856 * RAD_PER_DEG  # day-1
    a2 = 1.9171 * RAD_PER_DEG
    a3 = 0.98112
    sindelta = a0 * np.sin(a1 * (yday - 80) + a2 * (np.sin(a1 * yday) - a3))
    cosdelta = (1 - sindelta ** 2) ** 0.5

    # True Sun Time [degrees](=0 with sun in North, 15 deg/hour
    # b0 = 0.4083
    # b1 = 1.7958
    # b2 = 2.4875
    # b3 = 1.0712 * rad   # day-1
    # TST = (hours*15 + lon - b0*np.cos(a1*(yday-80)) -
    #        b1*np.cos(a1*(yday-80)) + b2*np.sin(b3*(yday-80)))

    # TST = 15 * hours  # Recover values from the fortran code

    # Simplified formula
    # correct at spring equinox (yday=80) neglecting +/- 3 deg = 12 min
    TST = hours * 15 + lon

    # Sun height  [degrees]
    # sinheight = sindelta*sin(phi) - cosdelta*cos(phi)*cos(15*hours*rad)
    sinheight = sindelta * np.sin(phi) - cosdelta * np.cos(phi) * np.cos(TST * RAD_PER_DEG)
    height = np.arcsin(sinheight) * DEG_PER_RAD

    return height
