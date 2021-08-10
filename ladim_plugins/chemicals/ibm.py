import numpy as np


class IBM:
    def __init__(self, config):
        self.D = config["ibm"].get('vertical_mixing', 0)  # Vertical mixing [m*2/s]
        self.dt = config['dt']
        self.vertdiff_dt = config["ibm"].get('vertdiff_dt', self.dt)  # Vertical diffusion timestep [s]
        self.x = np.array([])
        self.y = np.array([])
        self.pid = np.array([])
        self.land_collision = config["ibm"].get('land_collision', 'reposition')
        self.grid = None
        self.state = None
        self.forcing = None

    def update_ibm(self, grid, state, forcing):
        self.grid = grid
        self.state = state
        self.forcing = forcing

        self.advect()

        if isinstance(self.D, str):
            self.diffuse_ito()
        else:
            self.diffuse_const()

        if self.land_collision == "reposition":
            self.reposition()

    def advect(self):
        # Vertical advection
        x = self.state.X
        y = self.state.Y
        z = self.state.Z
        self.state.Z += self.dt * self.forcing.forcing.wvel(x, y, z)
        self.reflect()

    # Itô backwards scheme (LaBolle et al. 2000) for vertical diffusion
    def diffuse_ito(self):
        x = self.state.X
        y = self.state.Y
        H = self.grid.sample_depth(x, y)

        current_time = 0
        while current_time < self.dt:
            old_time = current_time
            current_time = np.minimum(self.dt, current_time + self.vertdiff_dt)
            ddt = current_time - old_time
            z = self.state.Z

            # Uniform stochastic differential
            dW = (np.random.rand(len(z)) * 2 - 1) * np.sqrt(3 * ddt)

            # Vertical diffusion, intermediate step
            diff_1 = self.forcing.forcing.vertdiff(x, y, z, self.D)
            Z1 = z + np.sqrt(2 * diff_1) * dW  # Diffusive step
            Z1[Z1 < 0] *= -1                    # Reflexive boundary at top
            below_seabed = Z1 > H
            Z1[below_seabed] = 2*H[below_seabed] - Z1[below_seabed]  # Reflexive bottom

            # Use intermediate step to sample diffusion
            diff_2 = self.forcing.forcing.vertdiff(x, y, Z1, self.D)

            # Diffusive step and reflective boundary conditions
            self.state.Z += np.sqrt(2 * diff_2) * dW  # Diffusive step
            self.reflect()

    def diffuse_const(self):
        # Uniform stochastic differential
        dW = (np.random.rand(len(self.state.Z)) * 2 - 1) * np.sqrt(3 * self.dt)
        self.state.Z += np.sqrt(2 * self.D) * dW
        self.reflect()

    def reflect(self):
        x = self.state.X
        y = self.state.Y
        z = self.state.Z
        H = self.grid.sample_depth(x, y)
        below_seabed = z > H
        z[z < 0] *= -1  # Reflexive boundary at top
        z[below_seabed] = 2 * H[below_seabed] - z[below_seabed]  # Reflexive bottom
        self.state.Z = z

    def reposition(self):
        # If particles have not moved: Assume they ended up on land.
        # If that is the case, reposition them within the cell.
        pid, pidx_old, pidx_new = np.intersect1d(self.pid, self.state.pid, return_indices=True)
        onland = ((self.x[pidx_old] == self.state.X[pidx_new]) &
                  (self.y[pidx_old] == self.state.Y[pidx_new]))
        num_onland = np.count_nonzero(onland)
        pidx_new_onland = pidx_new[onland]
        x_new = np.round(self.state.X[pidx_new_onland]) - 0.5 + np.random.rand(num_onland)
        y_new = np.round(self.state.Y[pidx_new_onland]) - 0.5 + np.random.rand(num_onland)
        self.state.X[pidx_new_onland] = x_new
        self.state.Y[pidx_new_onland] = y_new

        self.x = self.state.X
        self.y = self.state.Y
        self.pid = self.state.pid
