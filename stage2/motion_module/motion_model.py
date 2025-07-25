"""
motion model of trajectory, notice that objects of different categories often exhibit various motion patterns.
Five implemented motion models, including
- Two linear model: Constant Acceleration(CA), Constant Velocity(CV)
- Three non-linear model: Constant Turn Rate and Acceleration(CTRA), Constant Turn Rate and Velocity(CTRV), Bicycle Model
"""
import abc
import pdb
import numpy as np
import numba as nb
from typing import Tuple
from pyquaternion import Quaternion
from utils.math import warp_to_pi
from data.script.NUSC_CONSTANT import *
from pre_processing import concat_box_attr
import math

class ABC_MODEL(abc.ABC):
    """interface of all motion models
    """
    def __init__(self) -> None:
        self.SD = self.MD = -1
    
    @abc.abstractmethod
    def getInitState(self, det_infos: dict) -> np.mat:
        """from detection init tracklet

        Args:
            det_infos (dict): detection infos under different data format.
            {
                'nusc_box': NuscBox,
                'np_array': np.array,
                'has_velo': bool, whether the detetor has velocity info
            }

        Returns:
            np.mat: [state dim, 1], state vector
        """
        pass
    
    @abc.abstractmethod
    def getInitCovP(self, cls_label: int) -> np.mat:
        """init errorcov.

        Args:
            cls_label (int): set init errorcov category-specific. 

        Returns:
            np.mat: [state dim, state dim], Initialized covariance matrix
        """
        pass
    
    @abc.abstractmethod
    def getProcessNoiseQ(self, cls_label: int) -> np.mat:
        """get process noise matrix. The value set is somewhat arbitrary

        Args:
            cls_label (int): set init errorcov category-specific.

        Returns:
            np.mat: process noise matrix(fix)
        """
        pass
    
    @abc.abstractmethod
    def getTransitionF(self) -> np.mat:
        """get state transition matrix.
        obtain matrix in the motion_module/script/
        Returns:
            np.mat: [state dim, state dim], state transition matrix
        """
        pass
    
    @abc.abstractmethod
    def getMeaNoiseR(self, cls_label: int) -> np.mat:
        """get measurement noise matrix. The value set is also somewhat arbitrary

        Args:
            cls_label (int): set init errorcov category-specific.

        Returns:
            np.mat: measure noise matrix(fix)
        """
        pass
    
    @abc.abstractmethod
    def getMeaStateH(self) -> np.mat:
        """get state to measure transition matrix.
        obtain matrix in the motion_module/script
        Returns:
            np.mat: [measure dim, state dim], state to measure transition matrix
        """
        pass

    @abc.abstractmethod
    def getOutputInfo(self, state: np.mat) -> np.array:
        """convert state vector in the filter to the output format
        Note that, tra score will be process later
        Args:
            state (np.mat): [state dim, 1], predict or update state estimated by the filter

        Returns:
            np.array: [12(fix), 1], predict or update state under output file format
            output format: [x, y, z, w, l, h, vx, vy, ry(orientation, 1x4)]
        """
        pass

    def getMeasureInfo(self, det: dict = None) -> np.array:
        """convert det box to the measurement info for updating filter
        [x, y, (z, w, l, h, optional), (vx, vy, optional), ry]
        Args:
            det (dict, optional): same as self.init. Defaults to None.

        Returns:
            np.array: measurement for updating filter
        """
        if det is None: raise "detection cannot be None"

        mea_attr = ('center', 'wlh', 'velocity', 'yaw') if self.has_velo else ('center', 'wlh', 'yaw')
        list_det = concat_box_attr(det['nusc_box'], *mea_attr)

        if self.has_velo: list_det.pop(8)
        if self.has_geofilter: del list_det[2:6]

        # only for debug, delete on the release version
        # ensure the measure yaw goes around [0, 0, 1]
        try:
            assert list_det[-1] == det['nusc_box'].orientation.radians and det['nusc_box'].orientation.axis[-1] >= 0    
        except:
            assert list_det[-1] == det['nusc_box'].orientation.radians + math.pi or list_det[-1] == det['nusc_box'].orientation.radians - math.pi or list_det[-1] == det['nusc_box'].orientation.radians + math.pi*2 or list_det[-1] == det['nusc_box'].orientation.radians - math.pi*2
        try:
            assert len(list_det) == self.MD
        except:
            pdb.set_trace()

        return np.mat(list_det).T

    def getStateDim(self) -> int:
        return self.SD

    def getMeasureDim(self) -> int:
        return self.MD

class CTRA(ABC_MODEL):
    """Constant Acceleration and Turn Rate Motion Model
    Basic info:
        State vector: [x, y, (z, w, l, h, optional) v, a, ry, ry_rate]
        Measure vector: [x, y, (z, w, l, h, optional), (vx, vy, optional), ry]
    """
    def __init__(self, has_velo: bool, has_geofilter: bool, dt: float) -> None:
        super().__init__()
        self.has_velo, self.has_geofilter, self.dt = has_velo, has_geofilter, dt
        # state vector dim
        self.MD = 9 if self.has_velo else 7
        self.MD = (self.MD - 4) if self.has_geofilter else self.MD

        # measure vector dim
        self.SD = 6 if self.has_geofilter else 10

    def getInitState(self, det_infos: dict) -> np.mat:
        """from detection init tracklet
        Acceleration and yaw(turn) rate are both set to 0. when velociy
        on X/Y-Axis are available, the combined velocity is also set to 0
        """
        init_state = np.zeros(shape=self.SD)
        det, det_box = det_infos['np_array'], det_infos['nusc_box']

        if not self.has_geofilter:
            # set x, y, z, w, l, h, (v, if velo is valid), a, ry, ry_rate
            init_state[:6] = det[:6]
            if self.has_velo: init_state[6] = np.hypot(det[6], det[7])
        else:
            init_state[:2] = det[:2]
            if self.has_velo: init_state[2] = np.hypot(det[6], det[7])

        # set yaw
        init_state[-2] = det_box.yaw

        # only for debug
        q = Quaternion(det[8:12].tolist())
        q = -q if q.axis[-1] < 0 else q
        assert q.radians == det_box.yaw

        return np.mat(init_state).T

    def getInitCovP(self, cls_label: int) -> np.mat:
        """init errorcov. In general, when the speed is observable,
        the CTRA model can converge quickly, but when the speed is not measurable,
        we need to carefully set the initial covariance to help the model converge
        """
        if not self.has_velo:
            cls_name = CLASS_STR_TO_SEG_CLASS[cls_label]
            vector_p = CTRA_INIT_EFKP[cls_name] if cls_name in CTRA_INIT_EFKP else CTRA_INIT_EFKP['car']
        else:
            vector_p = CTRA_INIT_EFKP['car']

        if self.has_geofilter:
            vector_p = np.append(vector_p[:2], vector_p[-4:])
        return np.mat(np.diag(vector_p))

    def getProcessNoiseQ(self, cls_label) -> np.mat:
        """set process noise(fix)
        """
        return np.mat(np.eye(self.SD)) * 1

    def getMeaNoiseR(self, cls_label) -> np.mat:
        """set measure noise(fix)
        """
        return np.mat(np.eye(self.MD)) * 1

    def stateTransition(self, state: np.mat) -> np.mat:
        """state transition,
        obtain analytical solutions in the motion_module/script/CTRA_kinect_jacobian.ipynb
        Args:
            state (np.mat): [state dim, 1] the estimated state of the previous frame

        Returns:
            np.mat: [state dim, 1] the predict state of the current frame
        """
        assert state.shape == (10, 1) or state.shape == (6, 1), "state vector number in CTRA must equal to 10 or 6"

        dt = self.dt
        if not self.has_geofilter:
            x, y, z, w, l, h, v, a, theta, omega = state.T.tolist()[0]
        else:
            x, y, v, a, theta, omega = state.T.tolist()[0]

        yaw_sin, yaw_cos = np.sin(theta), np.cos(theta)
        next_v, next_ry = v + a * dt, theta + omega * dt

        # corner case(tiny yaw rate), prevent divide-by-zero overflow
        if abs(omega) < 0.001:
            displacement = v * dt + a * dt ** 2 / 2
            if not self.has_geofilter:
                predict_state = [x + displacement * yaw_cos,
                                 y + displacement * yaw_sin,
                                 z, w, l, h,
                                 next_v, a,
                                 next_ry, omega]
            else:
                predict_state = [x + displacement * yaw_cos,
                                 y + displacement * yaw_sin,
                                 next_v, a,
                                 next_ry, omega]
        else:
            ry_rate_inv_square = 1.0 / (omega * omega)
            next_yaw_sin, next_yaw_cos = np.sin(next_ry), np.cos(next_ry)
            if not self.has_geofilter:
                predict_state = [x + ry_rate_inv_square * (next_v * omega * next_yaw_sin + a * next_yaw_cos - v * omega * yaw_sin - a * yaw_cos),
                                 y + ry_rate_inv_square * (-next_v * omega * next_yaw_cos + a * next_yaw_sin + v * omega * yaw_cos - a * yaw_sin),
                                 z, w, l, h,
                                 next_v, a,
                                 next_ry, omega]
            else:
                predict_state = [x + ry_rate_inv_square * (next_v * omega * next_yaw_sin + a * next_yaw_cos - v * omega * yaw_sin - a * yaw_cos),
                                 y + ry_rate_inv_square * (-next_v * omega * next_yaw_cos + a * next_yaw_sin + v * omega * yaw_cos - a * yaw_sin),
                                 next_v, a,
                                 next_ry, omega]

        return np.mat(predict_state).T



    def StateToMeasure(self, state: np.mat) -> np.mat:
        """get state vector in the measure space
        state vector -> [x, y, (z, w, l, h, optional), v, a, ry, ry_rate]
        measure space -> [x, y, (z, w, l, h, optional), (vx, vy, optional), ry]

        Args:
            state (np.mat): [state dim, 1] the predict state of the current frame

        Returns:
            np.mat: [measure dim, 1] state vector projected in the measure space
        """
        assert state.shape == (10, 1) or state.shape == (6, 1), "state vector number in CTRA must equal to 10 or 6"

        if not self.has_geofilter:
            x, y, z, w, l, h, v, _, theta, _ = state.T.tolist()[0]
        else:
            x, y, v, _, theta, _ = state.T.tolist()[0]

        if self.has_velo:
            if not self.has_geofilter:
                state_info = [x, y, z,
                              w, l, h,
                              v * np.cos(theta),
                              v * np.sin(theta),
                              theta]
            else:
                state_info = [x, y,
                              v * np.cos(theta),
                              v * np.sin(theta),
                              theta]
        else:
            if not self.has_geofilter:
                state_info = [x, y, z,
                              w, l, h,
                              theta]
            else:
                state_info = [x, y, theta]

        return np.mat(state_info).T



    def getTransitionF(self, state: np.mat) -> np.mat:
        """obtain matrix in the motion_module/script/CTRA_kinect_jacobian.ipynb
        d(stateTransition) / d(state) at previous_state
        """
        dt = self.dt
        if not self.has_geofilter:
            _, _, _, _, _, _, v, a, theta, omega = state.T.tolist()[0]
        else:
            _, _, v, a, theta, omega = state.T.tolist()[0]

        yaw_sin, yaw_cos = np.sin(theta), np.cos(theta)

        # corner case, tiny turn rate
        if abs(omega) < 0.001:
            displacement = v * dt + a * dt ** 2 / 2
            if not self.has_geofilter:
                F = np.mat([[1, 0, 0, 0, 0, 0,  dt*yaw_cos,   dt**2*yaw_cos/2,        -displacement*yaw_sin,  0],
                            [0, 1, 0, 0, 0, 0,  dt*yaw_sin,   dt**2*yaw_sin/2,         displacement*yaw_cos,  0],
                            [0, 0, 1, 0, 0, 0,           0,                 0,                            0,  0],
                            [0, 0, 0, 1, 0, 0,           0,                 0,                            0,  0],
                            [0, 0, 0, 0, 1, 0,           0,                 0,                            0,  0],
                            [0, 0, 0, 0, 0, 1,           0,                 0,                            0,  0],
                            [0, 0, 0, 0, 0, 0,           1,                dt,                            0,  0],
                            [0, 0, 0, 0, 0, 0,           0,                 1,                            0,  0],
                            [0, 0, 0, 0, 0, 0,           0,                 0,                            1, dt],
                            [0, 0, 0, 0, 0, 0,           0,                 0,                            0,  1]])
            else:
                F = np.mat([[1, 0, dt * yaw_cos, dt ** 2 * yaw_cos / 2, -displacement * yaw_sin,  0],
                            [0, 1, dt * yaw_sin, dt ** 2 * yaw_sin / 2,  displacement * yaw_cos,  0],
                            [0, 0,            1,                    dt,                       0,  0],
                            [0, 0,            0,                     1,                       0,  0],
                            [0, 0,            0,                     0,                       1, dt],
                            [0, 0,            0,                     0,                       0,  1]])
        else:
            ry_rate_inv, ry_rate_inv_square, ry_rate_inv_cube = 1 / omega, 1 / (omega * omega), 1 / (omega * omega * omega)
            next_v, next_ry = v + a * dt, theta + omega * dt
            next_yaw_sin, next_yaw_cos = np.sin(next_ry), np.cos(next_ry)
            sin_diff, cos_diff = yaw_sin-next_yaw_sin, yaw_cos-next_yaw_cos
            if not self.has_geofilter:
                F = np.mat([[1, 0, 0, 0, 0, 0,  -ry_rate_inv*(sin_diff),   -ry_rate_inv_square*(cos_diff)+ry_rate_inv*dt*next_yaw_sin,        ry_rate_inv_square*a*(sin_diff)+ry_rate_inv*(next_v*next_yaw_cos-v*yaw_cos),  ry_rate_inv_cube*2*a*(cos_diff)+ry_rate_inv_square*(v*sin_diff-2*a*dt*next_yaw_sin)+ry_rate_inv*dt*next_v*next_yaw_cos ],
                            [0, 1, 0, 0, 0, 0,   ry_rate_inv*(cos_diff),   -ry_rate_inv_square*(sin_diff)-ry_rate_inv*dt*next_yaw_cos,        ry_rate_inv_square*a*(-cos_diff)+ry_rate_inv*(next_v*next_yaw_sin-v*yaw_sin), ry_rate_inv_cube*2*a*(sin_diff)+ry_rate_inv_square*(-v*cos_diff+2*a*dt*next_yaw_cos)+ry_rate_inv*dt*next_v*next_yaw_sin ],
                            [0, 0, 1, 0, 0, 0,           0,                 0,                            0,  0],
                            [0, 0, 0, 1, 0, 0,           0,                 0,                            0,  0],
                            [0, 0, 0, 0, 1, 0,           0,                 0,                            0,  0],
                            [0, 0, 0, 0, 0, 1,           0,                 0,                            0,  0],
                            [0, 0, 0, 0, 0, 0,           1,                dt,                            0,  0],
                            [0, 0, 0, 0, 0, 0,           0,                 1,                            0,  0],
                            [0, 0, 0, 0, 0, 0,           0,                 0,                            1, dt],
                            [0, 0, 0, 0, 0, 0,           0,                 0,                            0,  1]])
            else:
                F = np.mat([[1, 0, -ry_rate_inv*(sin_diff),   -ry_rate_inv_square*(cos_diff)+ry_rate_inv*dt*next_yaw_sin,        ry_rate_inv_square*a*(sin_diff)+ry_rate_inv*(next_v*next_yaw_cos-v*yaw_cos),  ry_rate_inv_cube*2*a*(cos_diff)+ry_rate_inv_square*(v*sin_diff-2*a*dt*next_yaw_sin)+ry_rate_inv*dt*next_v*next_yaw_cos ],
                            [0, 1,  ry_rate_inv*(cos_diff),   -ry_rate_inv_square*(sin_diff)-ry_rate_inv*dt*next_yaw_cos,        ry_rate_inv_square*a*(-cos_diff)+ry_rate_inv*(next_v*next_yaw_sin-v*yaw_sin), ry_rate_inv_cube*2*a*(sin_diff)+ry_rate_inv_square*(v*next_yaw_cos-v*yaw_cos+2*a*dt*next_yaw_cos)+ry_rate_inv*dt*next_v*next_yaw_sin ],
                            [0, 0, 1, dt, 0,  0],
                            [0, 0, 0,  1, 0,  0],
                            [0, 0, 0,  0, 1, dt],
                            [0, 0, 0,  0, 0,  1]])

        return F

    def getMeaStateH(self, state: np.mat) -> np.mat:
        """obtain matrix in the motion_module/script/CTRA_kinect_jacobian.ipynb
        d(StateToMeasure) / d(state) at predict_state
        """
        if not self.has_geofilter:
            _, _, _, _, _, _, v, _, theta, _ = state.T.tolist()[0]
        else:
            _, _, v, _, theta, _ = state.T.tolist()[0]

        if self.has_velo:
            yaw_sin, yaw_cos = np.sin(theta), np.cos(theta)
            if not self.has_geofilter:
                H = np.mat([[1, 0, 0, 0, 0, 0,        0, 0,           0, 0],
                            [0, 1, 0, 0, 0, 0,        0, 0,           0, 0],
                            [0, 0, 1, 0, 0, 0,        0, 0,           0, 0],
                            [0, 0, 0, 1, 0, 0,        0, 0,           0, 0],
                            [0, 0, 0, 0, 1, 0,        0, 0,           0, 0],
                            [0, 0, 0, 0, 0, 1,        0, 0,           0, 0],
                            [0, 0, 0, 0, 0, 0, yaw_cos,  0,  -v*yaw_sin, 0],
                            [0, 0, 0, 0, 0, 0, yaw_sin,  0,   v*yaw_cos, 0],
                            [0, 0, 0, 0, 0, 0,        0, 0,           1, 0]])
            else:
                H = np.mat([[1, 0,         0, 0,            0, 0],
                            [0, 1,         0, 0,            0, 0],
                            [0, 0,   yaw_cos, 0,   -v*yaw_sin, 0],
                            [0, 0,   yaw_sin, 0,    v*yaw_cos, 0],
                            [0, 0,         0, 0,            1, 0]])
        else:
            if not self.has_geofilter:
                H = np.mat([[1, 0, 0, 0, 0, 0,        0, 0,           0, 0],
                            [0, 1, 0, 0, 0, 0,        0, 0,           0, 0],
                            [0, 0, 1, 0, 0, 0,        0, 0,           0, 0],
                            [0, 0, 0, 1, 0, 0,        0, 0,           0, 0],
                            [0, 0, 0, 0, 1, 0,        0, 0,           0, 0],
                            [0, 0, 0, 0, 0, 1,        0, 0,           0, 0],
                            [0, 0, 0, 0, 0, 0,        0, 0,           1, 0]])
            else:
                H = np.mat([[1, 0,         0, 0,           0, 0],
                            [0, 1,         0, 0,           0, 0],
                            [0, 0,         0, 0,           1, 0]])
        return H

    def getOutputInfo(self, state: np.mat) -> np.array:
        """convert state vector in the filter to the output format
        Note that, tra score will be process later
        """
        rotation, v, yaw = Quaternion(axis=(0, 0, 1), radians=state[-2, 0]).q, state[-4, 0], state[-2, 0]

        if not self.has_geofilter:
            list_state = state.T.tolist()[0][:6] + [v * np.cos(yaw), v * np.sin(yaw)] + rotation.tolist()
        else:
            list_state = state.T.tolist()[0][:2] + [-1, -1, -1, -1] + [v * np.cos(yaw), v * np.sin(yaw)] + rotation.tolist()

        return np.array(list_state)

    @staticmethod
    def warpResYawToPi(res: np.mat) -> np.mat:
        """warp res yaw to [-pi, pi) in place

        Args:
            res (np.mat): [measure dim, 1]
            res infos -> [x, y, z, w, l, h, (vx, vy, optional), ry]

        Returns:
            np.mat: [measure dim, 1], residual warped to [-pi, pi)
        """
        res[-1, 0] = warp_to_pi(res[-1, 0])
        return res

    @staticmethod
    def warpStateYawToPi(state: np.mat) -> np.mat:
        """warp state yaw to [-pi, pi) in place

        Args:
            state (np.mat): [state dim, 1]
            State vector: [x, y, z, w, l, h, v, a, ry, ry_rate]

        Returns:
            np.mat: [state dim, 1], state after warping
        """
        state[-2, 0] = warp_to_pi(state[-2, 0])
        return state

class BICYCLE(ABC_MODEL):
    """Constant Acceleration and Turn Rate Motion Model
    Basic info:
        State vector: [x_gra, y_gra, (z_geo, w, l, h, optional), v, a, ry, sigma]
        Measure vector: [x_geo, y_geo, (z_geo, w, l, h, optional), (vx, vy, optional), ry]
    Important assumptions:
        1. Although the acceleration interface is reserved,
        we still think that the velocity is constant when
        beta is large. In other cases, the object is considered
        to be moving in a straight line with uniform acceleration.
        2. Based on experience, we set two hyperparameters here,
        wheelbase ratio is set to 0.8, rear tire ratio is set to 0.5.
        3. the steering angle(sigma) is also considered to be constant.
        4. x, y described in the filter are gravity center, however,
        the infos we measure are geometric center. Transformation
        process is required.
        5. We don't integrate variable 'length' to the jacobian matrix
        for simple and fast solution
    """
    def __init__(self, has_velo: bool, has_geofilter: bool, dt: float) -> None:
        super().__init__()
        self.has_velo, self.has_geofilter, self.dt = has_velo, has_geofilter, dt
        self.w_r, self.lf_r = 0.8, 0.5

        # Measure vector -> [x_geo, y_geo, (z_geo, w, optional) l, (h, optional), (vx, vy, optional), ry]
        self.MD = 9 if self.has_velo else 7
        if self.has_geofilter: self.MD -= 3

        # State vector -> [x_gra, y_gra, (z_geo, w, optional), l, (h, optional), v, a, ry, sigma]
        self.SD = 10 if not self.has_geofilter else 7

    def getInitState(self, det_infos: dict) -> np.mat:
        """from detection init tracklet, we set some assumptions in
        the BICYCLE model for easy calculation
        """
        init_state = np.zeros(shape=self.SD)
        det, det_box = det_infos['np_array'], det_infos['nusc_box']

        # set gravity center x, y
        init_state[:2] = self.geoCenterToGraCenter(geo_center=[det[0], det[1]],
                                                   theta=det_box.yaw,
                                                   length=det[4])

        if not self.has_geofilter:
            # set z, w, l, h, (v, if velo is valid)
            init_state[2:6] = det[2:6]
            if self.has_velo: init_state[6] = np.hypot(det[6], det[7])
        else:
            # set l, (v, if velo is valid)
            init_state[2] = det[4]  # length
            if self.has_velo: init_state[-4] = np.hypot(det[6], det[7])  # velocity

        # set yaw
        init_state[-2] = det_box.yaw

        # only for debug
        q = Quaternion(det[8:12].tolist())
        q = -q if q.axis[-1] < 0 else q
        assert q.radians == det_box.yaw

        return np.mat(init_state).T

    def getInitCovP(self, cls_label: int) -> np.mat:
        """init errorcov. In the BICYCLE motion model, we apply
        same errorcov initialization strategy as the CTRA model.
        """
        if not self.has_velo:
            cls_name = CLASS_STR_TO_SEG_CLASS[cls_label]
            vector_p = BIC_INIT_EKFP[cls_name] if cls_name in BIC_INIT_EKFP else BIC_INIT_EKFP['bicycle']
        else:
            vector_p = BIC_INIT_EKFP['bicycle'] if cls_label == 0 else [10, 10, 10, 10, 10, 10, 1000, 10, 10, 10]

        if self.has_geofilter: vector_p = [10, 10, 10, 1000, 10, 10, 10]

        return np.mat(np.diag(vector_p))

    def getProcessNoiseQ(self, cls_label) -> np.mat:
        """set process noise(fix)
        """
        Q = np.mat(np.eye(self.SD)) * FINETUNE_Q[cls_label]
        Q[-2, -2] /= 2
        Q[-4, -4] *= 2
        return Q

    def getMeaNoiseR(self, cls_label) -> np.mat:
        """set measure noise(fix)
        """
        R = np.mat(np.eye(self.MD)) * FINETUNE_R[cls_label]
        R[-1, -1] /= 2
        if self.has_velo:
            R[-2, -2] *= 2
            R[-3, -3] *= 2
        return R

    def getTransitionF(self, state: np.mat) -> np.mat:
        """obtain matrix in the motion_module/script/BIC_kinect_jacobian.ipynb
        d(stateTransition) / d(state) at previous_state
        """

        dt = self.dt
        if not self.has_geofilter:
            _, _, _, _, l, _, v, a, theta, sigma = state.T.tolist()[0]
        else:
            _, _, l, v, a, theta, sigma = state.T.tolist()[0]

        beta, _, lr = self.getBicBeta(l, sigma)

        sin_yaw, cos_yaw = np.sin(theta), np.cos(theta)

        # corner case, tiny beta
        if abs(beta) < 0.001:
            displacement = a*dt**2/2 + dt*v
            if not self.has_geofilter:
                F = np.mat([[1, 0, 0, 0, 0, 0,  dt*cos_yaw,  dt**2*cos_yaw/2,        -displacement*sin_yaw, 0],
                            [0, 1, 0, 0, 0, 0,  dt*sin_yaw,  dt**2*sin_yaw/2,         displacement*cos_yaw, 0],
                            [0, 0, 1, 0, 0, 0,           0,                0,                            0, 0],
                            [0, 0, 0, 1, 0, 0,           0,                0,                            0, 0],
                            [0, 0, 0, 0, 1, 0,           0,                0,                            0, 0],
                            [0, 0, 0, 0, 0, 1,           0,                0,                            0, 0],
                            [0, 0, 0, 0, 0, 0,           1,               dt,                            0, 0],
                            [0, 0, 0, 0, 0, 0,           0,                1,                            0, 0],
                            [0, 0, 0, 0, 0, 0,           0,                0,                            1, 0],
                            [0, 0, 0, 0, 0, 0,           0,                0,                            0, 1]])
            else:
                F = np.mat([[1, 0,  0,   dt*cos_yaw,  dt**2*cos_yaw/2,        -displacement*sin_yaw, 0],
                            [0, 1,  0,   dt*sin_yaw,  dt**2*sin_yaw/2,         displacement*cos_yaw, 0],
                            [0, 0,  1,            0,                0,                            0, 0],
                            [0, 0,  0,            1,               dt,                            0, 0],
                            [0, 0,  0,            0,                1,                            0, 0],
                            [0, 0,  0,            0,                0,                            1, 0],
                            [0, 0,  0,            0,                0,                            0, 1]])
        else:
            next_yaw, sin_beta = theta + v / lr * np.sin(beta) * dt, np.sin(beta)
            v_yaw, next_v_yaw = beta + theta, beta + next_yaw
            if not self.has_geofilter:
                F = np.mat([[1, 0, 0, 0, 0, 0, dt*np.cos(next_v_yaw), 0, -lr*np.cos(v_yaw)/sin_beta + lr*np.cos(next_v_yaw)/sin_beta, 0],
                            [0, 1, 0, 0, 0, 0, dt*np.sin(next_v_yaw), 0, -lr*np.sin(v_yaw)/sin_beta + lr*np.sin(next_v_yaw)/sin_beta, 0],
                            [0, 0, 1, 0, 0, 0,                     0, 0,                                                           0, 0],
                            [0, 0, 0, 1, 0, 0,                     0, 0,                                                           0, 0],
                            [0, 0, 0, 0, 1, 0,                     0, 0,                                                           0, 0],
                            [0, 0, 0, 0, 0, 1,                     0, 0,                                                           0, 0],
                            [0, 0, 0, 0, 0, 0,                     1, 0,                                                           0, 0],
                            [0, 0, 0, 0, 0, 0,                     0, 0,                                                           0, 0],
                            [0, 0, 0, 0, 0, 0,        dt/lr*sin_beta, 0,                                                           1, 0],
                            [0, 0, 0, 0, 0, 0,                     0, 0,                                                           0, 1]])
            else:
                F = np.mat([[1, 0,  0,  dt*np.cos(next_v_yaw), 0, -lr*np.cos(v_yaw)/sin_beta + lr*np.cos(next_v_yaw)/sin_beta, 0],
                            [0, 1,  0,  dt*np.sin(next_v_yaw), 0, -lr*np.sin(v_yaw)/sin_beta + lr*np.sin(next_v_yaw)/sin_beta, 0],
                            [0, 0,  1,                      0, 0,                                                           0, 0],
                            [0, 0,  0,                      1, 0,                                                           0, 0],
                            [0, 0,  0,                      0, 0,                                                           0, 0],
                            [0, 0,  0,         dt/lr*sin_beta, 0,                                                           1, 0],
                            [0, 0,  0,                      0, 0,                                                           0, 1]])

        return F

    def getMeaStateH(self, state: np.mat) -> np.mat:
        """obtain matrix in the motion_module/script/BIC_kinect_jacobian.ipynb
        d(StateToMeasure) / d(state) at predict_state
        """
        if not self.has_geofilter:
            _, _, _, _, l, _, v, _, theta, sigma = state.T.tolist()[0]
        else:
            _, _, l, v, _, theta, sigma = state.T.tolist()[0]

        geo2gra_dist, lr2l = self.graToGeoDist(l), self.w_r * (0.5 - self.lf_r)
        sin_yaw, cos_yaw = np.sin(theta), np.cos(theta)

        if self.has_velo:
            beta, _, _ = self.getBicBeta(l, sigma)
            v_yaw = beta + theta
            sin_v_yaw, cos_v_yaw = np.sin(v_yaw), np.cos(v_yaw)
            if not self.has_geofilter:
                H = np.mat([[1, 0, 0, 0, -lr2l*cos_yaw, 0,               0, 0,  geo2gra_dist*sin_yaw, 0],
                            [0, 1, 0, 0, -lr2l*sin_yaw, 0,               0, 0, -geo2gra_dist*cos_yaw, 0],
                            [0, 0, 1, 0,             0, 0,               0, 0,                     0, 0],
                            [0, 0, 0, 1,             0, 0,               0, 0,                     0, 0],
                            [0, 0, 0, 0,             1, 0,               0, 0,                     0, 0],
                            [0, 0, 0, 0,             0, 1,               0, 0,                     0, 0],
                            [0, 0, 0, 0,             0, 0,       cos_v_yaw, 0,          -v*sin_v_yaw, 0],
                            [0, 0, 0, 0,             0, 0,       sin_v_yaw, 0,           v*cos_v_yaw, 0],
                            [0, 0, 0, 0,             0, 0,               0, 0,                     1, 0]])
            else:
                H = np.mat([[1, 0,  -lr2l*cos_yaw,                0, 0,  geo2gra_dist*sin_yaw, 0],
                            [0, 1,  -lr2l*sin_yaw,                0, 0, -geo2gra_dist*cos_yaw, 0],
                            [0, 0,              1,                0, 0,                     0, 0],
                            [0, 0,              0,        cos_v_yaw, 0,          -v*sin_v_yaw, 0],
                            [0, 0,              0,        sin_v_yaw, 0,           v*cos_v_yaw, 0],
                            [0, 0,              0,                0, 0,                     1, 0]])
        else:
            if not self.has_geofilter:
                H = np.mat([[1, 0, 0, 0, -lr2l*cos_yaw, 0,               0, 0,  geo2gra_dist*sin_yaw, 0],
                            [0, 1, 0, 0, -lr2l*sin_yaw, 0,               0, 0, -geo2gra_dist*cos_yaw, 0],
                            [0, 0, 1, 0,             0, 0,               0, 0,                     0, 0],
                            [0, 0, 0, 1,             0, 0,               0, 0,                     0, 0],
                            [0, 0, 0, 0,             1, 0,               0, 0,                     0, 0],
                            [0, 0, 0, 0,             0, 1,               0, 0,                     0, 0],
                            [0, 0, 0, 0,             0, 0,               0, 0,                     1, 0]])
            else:
                H = np.mat([[1, 0,  -lr2l*cos_yaw,                0, 0,  geo2gra_dist*sin_yaw, 0],
                            [0, 1,  -lr2l*sin_yaw,                0, 0, -geo2gra_dist*cos_yaw, 0],
                            [0, 0,              1,                0, 0,                     0, 0],
                            [0, 0,              0,                0, 0,                     1, 0]])

        return H

    def stateTransition(self, state: np.mat) -> np.mat:
        """State transition based on model assumptions
        """
        assert state.shape == (10, 1) or state.shape == (7, 1), "state vector number in BICYCLE must equal to 10 or 7"

        dt = self.dt
        if not self.has_geofilter:
            x_gra, y_gra, z, w, l, h, v, a, theta, sigma = state.T.tolist()[0]
        else:
            x_gra, y_gra, l, v, a, theta, sigma = state.T.tolist()[0]

        beta, _, lr = self.getBicBeta(l, sigma)

        # corner case, tiny yaw rate
        if abs(beta) > 0.001:
            next_yaw = theta + v / lr * np.sin(beta) * dt
            v_yaw, next_v_yaw = beta + theta, beta + next_yaw
            if not self.has_geofilter:
                predict_state = [x_gra + (lr * (np.sin(next_v_yaw) - np.sin(v_yaw))) / np.sin(beta),  # x_gra
                                 y_gra - (lr * (np.cos(next_v_yaw) - np.cos(v_yaw))) / np.sin(beta),  # y_gra
                                 z, w, l, h,
                                 v, 0, next_yaw, sigma]
            else:
                predict_state = [x_gra + (lr * (np.sin(next_v_yaw) - np.sin(v_yaw))) / np.sin(beta),  # x_gra
                                 y_gra - (lr * (np.cos(next_v_yaw) - np.cos(v_yaw))) / np.sin(beta),  # y_gra
                                 l, v, 0, next_yaw, sigma]
        else:
            displacement = v * dt + a * dt ** 2 / 2
            if not self.has_geofilter:
                predict_state = [x_gra + displacement * np.cos(theta),
                                 y_gra + displacement * np.sin(theta),
                                 z, w, l, h,
                                 v + a * dt, a, theta, sigma]
            else:
                predict_state = [x_gra + displacement * np.cos(theta),
                                 y_gra + displacement * np.sin(theta),
                                 l, v + a * dt, a, theta, sigma]

        return np.mat(predict_state).T

    def StateToMeasure(self, state: np.mat) -> np.mat:
        """get state vector in the measure space
        """
        assert state.shape == (10, 1) or state.shape == (7, 1), "state vector number in BICYCLE must equal to 10 or 7"

        if not self.has_geofilter:
            x_gra, y_gra, z, w, l, h, v, _, theta, sigma = state.T.tolist()[0]
        else:
            x_gra, y_gra, l, v, _, theta, sigma = state.T.tolist()[0]

        beta, _, _ = self.getBicBeta(l, sigma)
        geo2gra_dist = self.graToGeoDist(l)

        if self.has_velo:
            if not self.has_geofilter:
                meas_state = [x_gra - geo2gra_dist * np.cos(theta),
                              y_gra - geo2gra_dist * np.sin(theta),
                              z, w, l, h,
                              v * np.cos(theta + beta),
                              v * np.sin(theta + beta),
                              theta]
            else:
                meas_state = [x_gra - geo2gra_dist * np.cos(theta),
                              y_gra - geo2gra_dist * np.sin(theta),
                              l,
                              v * np.cos(theta + beta),
                              v * np.sin(theta + beta),
                              theta]
        else:
            if not self.has_geofilter:
                meas_state = [x_gra - geo2gra_dist * np.cos(theta),
                              y_gra - geo2gra_dist * np.sin(theta),
                              z, w, l, h,
                              theta]
            else:
                meas_state = [x_gra - geo2gra_dist * np.cos(theta),
                              y_gra - geo2gra_dist * np.sin(theta),
                              l,
                              theta]

        return np.mat(meas_state).T

    def getBicBeta(self, length: float, sigma: float) -> Tuple[float, float, float]:
        """get the angle between the object velocity and the
        X-axis of the coordinate system

        Args:
            length (float): object length
            sigma (float): the steering angle, radians

        Returns:
            float: the angle between the object velocity and X-axis, radians
        """

        lf, lr = length * self.w_r * self.lf_r, length * self.w_r * (1 - self.lf_r)
        beta = np.arctan(lr / (lr + lf) * np.tan(sigma))
        return beta, lf, lr


    def geoCenterToGraCenter(self, geo_center: list, theta: float, length: float) -> np.ndarray:
        """from geo center to gra center

        Args:
            geo_center (list): object geometric center
            theta (float): object heading yaw
            length (float): object length

        Returns:
            np.ndarray: object gravity center
        """
        geo2gra_dist = self.graToGeoDist(length)
        gra_center = [geo_center[0] + geo2gra_dist * np.cos(theta),
                      geo_center[1] + geo2gra_dist * np.sin(theta)]
        return np.array(gra_center)

    def graCenterToGeoCenter(self, gra_center: list, theta: float, length: float) -> np.ndarray:
        """from gra center to geo center

        Args:
            gra_center (list): object gravity center
            theta (float): object heading yaw
            length (float): object length

        Returns:
            np.ndarray: object geometric center
        """
        geo2gra_dist = self.graToGeoDist(length)
        geo_center = [gra_center[0] - geo2gra_dist * np.cos(theta),
                      gra_center[1] - geo2gra_dist * np.sin(theta)]
        return np.array(geo_center)

    def graToGeoDist(self, length: float) -> float:
        """get gra center to geo center distance

        Args:
            length (float): object length

        Returns:
            float: gra center to geo center distance
        """
        return length * self.w_r * (0.5 - self.lf_r)

    def getOutputInfo(self, state: np.mat) -> np.array:
        """convert state vector in the filter to the output format
        Note that, tra score will be process later
        """
        if not self.has_geofilter:
            x_gra, y_gra, z, w, l, h, v, _, theta, sigma = state.T.tolist()[0]
        else:
            x_gra, y_gra, l, v, _, theta, sigma = state.T.tolist()[0]

        beta, _, _ = self.getBicBeta(l, sigma)

        rotation = Quaternion(axis=(0, 0, 1), radians=theta).q
        geo_center = self.graCenterToGeoCenter(gra_center=[x_gra, y_gra],
                                               theta=theta,
                                               length=l)

        if not self.has_geofilter:
            list_state = geo_center.tolist() + state.T.tolist()[0][2:8] + rotation.tolist()
        else:
            list_state = geo_center.tolist() + [-1, -1, -1, -1, v * np.cos(theta + beta), v * np.sin(theta + beta)] + rotation.tolist()

        return np.array(list_state)

    def getMeasureInfo(self, det: dict = None) -> np.array:
        """get specific measure infos from raw det
        """
        if det is None: raise "detection cannot be None"

        mea_attr = ('center', 'wlh', 'velocity', 'yaw') if self.has_velo else ('center', 'wlh', 'yaw')
        try:
            list_det = concat_box_attr(det['nusc_box'], *mea_attr)
        except:
            pdb.set_trace()

        if self.has_velo: list_det.pop(8)

        if self.has_geofilter:
            del list_det[2:4]  # delete z, w
            del list_det[3]    # delete h

        # only for debug, delete on the release version
        # ensure the measure yaw goes around [0, 0, 1]
        try:
            assert list_det[-1] == det['nusc_box'].orientation.radians and det['nusc_box'].orientation.axis[-1] >= 0    
        except:
            assert list_det[-1] == det['nusc_box'].orientation.radians + math.pi or list_det[-1] == det['nusc_box'].orientation.radians - math.pi or list_det[-1] == det['nusc_box'].orientation.radians + math.pi*2 or list_det[-1] == det['nusc_box'].orientation.radians - math.pi*2
        assert len(list_det) == self.MD

        return np.mat(list_det).T

    @staticmethod
    def warpResYawToPi(res: np.mat) -> np.mat:
        """warp res yaw to [-pi, pi) in place

        Args:
            res (np.mat): [measure dim, 1]
            res infos -> [x, y, z, w, l, h, (vx, vy, optional), ry]

        Returns:
            np.mat: [measure dim, 1], residual warped to [-pi, pi)
        """
        res[-1, 0] = warp_to_pi(res[-1, 0])
        return res

    @staticmethod
    def warpStateYawToPi(state: np.mat) -> np.mat:
        """warp state yaw to [-pi, pi) in place

        Args:
            state (np.mat): [state dim, 1]
            State vector: [x, y, z, w, l, h, v, a, ry, sigma]

        Returns:
            np.mat: [state dim, 1], state after warping
        """
        state[-2, 0] = warp_to_pi(state[-2, 0])
        return state

class CV(ABC_MODEL):
    """Constant Velocity Motion Model
    Basic info:
        State vector: [x, y, (z, w, l, h, optional) vx, vy, vz, ry]
        Measure vector: [x, y, (z, w, l, h, optional) (vx, vy, optional), ry]
    """

    def __init__(self, has_velo: bool, has_geofilter: bool, dt: float) -> None:
        super().__init__()
        self.has_velo, self.has_geofilter, self.dt = has_velo, has_geofilter, dt
        self.MD = 9 if self.has_velo else 7
        self.MD = (self.MD - 4) if self.has_geofilter else self.MD

        # measure vector dim
        self.SD = 6 if self.has_geofilter else 10

    def getInitState(self, det_infos: dict) -> np.mat:
        """from detection init tracklet
        Velocity on the z-axis are set to 0
        """
        init_state = np.zeros(shape=self.SD)
        det, det_box = det_infos['np_array'], det_infos['nusc_box']

        if not self.has_geofilter:
            # set x, y, z, w, l, h, (vx, vy, if velo is valid)
            init_state[:6] = det[:6]
            if self.has_velo: init_state[6:8] = det[6:8]
        else:
            init_state[:2] = det[:2]
            if self.has_velo: init_state[2:4] = det[6:8]

        # set yaw
        init_state[-1] = det_box.yaw

        # only for debug
        q = Quaternion(det[8:12].tolist())
        q = -q if q.axis[-1] < 0 else q
        assert q.radians == det_box.yaw

        return np.mat(init_state).T

    def getInitCovP(self, cls_label: int) -> np.mat:
        """init errorcov. Generally, the CV model can converge quickly,
        so not particularly sensitive to initialization
        """
        return np.mat(np.eye(self.SD)) * 0.01

    def getProcessNoiseQ(self, cls_label) -> np.mat:
        """set process noise(fix)
        """
        Q = np.mat(np.eye(self.SD)) * FINETUNE_Q[cls_label]
        Q[-1, -1] /= 2
        Q[-4, -4] *= 2
        Q[-3, -3] *= 2
        return Q

    def getMeaNoiseR(self, cls_label) -> np.mat:
        """set measure noise(fix)
        """
        R = np.mat(np.eye(self.MD)) * FINETUNE_R[cls_label]
        R[-1, -1] /= 2
        if self.has_velo:
            R[-2, -2] *= 2
            R[-3, -3] *= 2
        return R

    def getTransitionF(self) -> np.mat:
        """obtain matrix in the motion_module/script/CV_kinect_jacobian.ipynb
        """
        dt = self.dt
        if not self.has_geofilter:
            F = np.mat([[1, 0, 0, 0, 0, 0, dt, 0, 0, 0],
                        [0, 1, 0, 0, 0, 0, 0, dt, 0, 0],
                        [0, 0, 1, 0, 0, 0, 0, 0, 0, 0],
                        [0, 0, 0, 1, 0, 0, 0, 0, 0, 0],
                        [0, 0, 0, 0, 1, 0, 0, 0, 0, 0],
                        [0, 0, 0, 0, 0, 1, 0, 0, 0, 0],
                        [0, 0, 0, 0, 0, 0, 1, 0, 0, 0],
                        [0, 0, 0, 0, 0, 0, 0, 1, 0, 0],
                        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                        [0, 0, 0, 0, 0, 0, 0, 0, 0, 1]])
        else:
            F = np.mat([[1, 0, dt, 0, 0, 0],
                        [0, 1, 0, dt, 0, 0],
                        [0, 0, 1, 0, 0, 0],
                        [0, 0, 0, 1, 0, 0],
                        [0, 0, 0, 0, 0, 0],
                        [0, 0, 0, 0, 0, 1]])
        return F

    def getMeaStateH(self) -> np.mat:
        """obtain matrix in the motion_module/script/CV_kinect_jacobian.ipynb
        """
        if self.has_velo:
            if not self.has_geofilter:
                H = np.mat([[1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 1, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 1, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 1, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 1, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 1, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 0, 1, 0, 0, 0],
                            [0, 0, 0, 0, 0, 0, 0, 1, 0, 0],
                            [0, 0, 0, 0, 0, 0, 0, 0, 0, 1]])
            else:
                H = np.mat([[1, 0, 0, 0, 0, 0],
                            [0, 1, 0, 0, 0, 0],
                            [0, 0, 1, 0, 0, 0],
                            [0, 0, 0, 1, 0, 0],
                            [0, 0, 0, 0, 0, 1]])
        else:
            if not self.has_geofilter:
                H = np.mat([[1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 1, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 1, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 1, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 1, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 1, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 0, 0, 0, 0, 1]])
            else:
                H = np.mat([[1, 0, 0, 0, 0, 0],
                            [0, 1, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 1]])
        return H

    def getOutputInfo(self, state: np.mat) -> np.array:
        """convert state vector in the filter to the output format
        Note that, tra score will be process later
        """
        rotation = Quaternion(axis=(0, 0, 1), radians=state[-1, 0]).q
        if not self.has_geofilter:
            list_state = state.T.tolist()[0][:8] + rotation.tolist()
        else:
            list_state = state.T.tolist()[0][:2] + [-1, -1, -1, -1] + state.T.tolist()[0][2:4] + rotation.tolist()
        return np.array(list_state)

    @staticmethod
    def warpResYawToPi(res: np.mat) -> np.mat:
        """warp res yaw to [-pi, pi) in place
        Args:
            res (np.mat): [measure dim, 1]
            res infos -> [x, y, z, w, l, h, (vx, vy, optional), ry]
        Returns:
            np.mat: [measure dim, 1], residual warped to [-pi, pi)
        """
        res[-1, 0] = warp_to_pi(res[-1, 0])
        return res

    @staticmethod
    def warpStateYawToPi(state: np.mat) -> np.mat:
        """warp state yaw to [-pi, pi) in place
        Args:
            state (np.mat): [state dim, 1]
            State vector: [x, y, z, w, l, h, vx, vy, vz, ry]
        Returns:
            np.mat: [state dim, 1], state after warping
        """
        state[-1, 0] = warp_to_pi(state[-1, 0])
        return state

class CA(ABC_MODEL):
    """Constant Acceleration Motion Model
    Basic info:
        State vector: [x, y, (z, w, l, h, optional) vx, vy, vz, ax, ay, az, ry]
        Measure vector: [x, y, (z, w, l, h, optional), (vx, vy, optional), ry]
    """
    def __init__(self, has_velo: bool, has_geofilter: bool, dt: float) -> None:
        super().__init__()
        self.has_velo, self.has_geofilter, self.dt = has_velo, has_geofilter, dt
        # state vector dim
        self.MD = 9 if self.has_velo else 7
        self.MD = (self.MD - 4) if self.has_geofilter else self.MD

        # measure vector dim
        self.SD = 9 if self.has_geofilter else 13

    def getInitState(self, det_infos: dict) -> np.mat:
        """from detection init tracklet
        Acceleration and velocity on the z-axis are both set to 0
        """
        init_state = np.zeros(shape=self.SD)
        det, det_box = det_infos['np_array'], det_infos['nusc_box']

        if not self.has_geofilter:
            # set x, y, z, w, l, h, (vx, vy, if velo is valid)
            init_state[:6] = det[:6]
            if self.has_velo: init_state[6:8] = det[6:8]
        else:
            init_state[:2] = det[:2]
            if self.has_velo: init_state[2:4] = det[6:8]

        # set yaw
        init_state[-1] = det_box.yaw

        # only for debug
        q = Quaternion(det[8:12].tolist())
        q = -q if q.axis[-1] < 0 else q
        assert q.radians == det_box.yaw

        return np.mat(init_state).T

    def getInitCovP(self, cls_label: int) -> np.mat:
        """init errorcov. Generally, the CA model can converge quickly,
        so not particularly sensitive to initialization
        """
        return np.mat(np.eye(self.SD)) * 0.01

    def getProcessNoiseQ(self, cls_label) -> np.mat:
        """set process noise(fix)
        """
        return np.mat(np.eye(self.SD)) * 100

    def getMeaNoiseR(self, cls_label) -> np.mat:
        """set measure noise(fix)
        """
        return np.mat(np.eye(self.MD)) * 0.001

    def getTransitionF(self) -> np.mat:
        """obtain matrix in the motion_module/script/Linear_kinect_jacobian.ipynb
        """
        dt = self.dt
        if not self.has_geofilter:
            F = np.mat([[1, 0, 0, 0, 0, 0, dt,  0, 0, 0.5*dt**2,         0,  0, 0],
                        [0, 1, 0, 0, 0, 0,  0, dt, 0,         0, 0.5*dt**2,  0, 0],
                        [0, 0, 1, 0, 0, 0,  0,  0, 0,         0,         0,  0, 0],
                        [0, 0, 0, 1, 0, 0,  0,  0, 0,         0,         0,  0, 0],
                        [0, 0, 0, 0, 1, 0,  0,  0, 0,         0,         0,  0, 0],
                        [0, 0, 0, 0, 0, 1,  0,  0, 0,         0,         0,  0, 0],
                        [0, 0, 0, 0, 0, 0,  1,  0, 0,        dt,         0,  0, 0],
                        [0, 0, 0, 0, 0, 0,  0,  1, 0,         0,        dt,  0, 0],
                        [0, 0, 0, 0, 0, 0,  0,  0, 0,         0,         0,  0, 0],
                        [0, 0, 0, 0, 0, 0,  0,  0, 0,         1,         0,  0, 0],
                        [0, 0, 0, 0, 0, 0,  0,  0, 0,         0,         1,  0, 0],
                        [0, 0, 0, 0, 0, 0,  0,  0, 0,         0,         0,  0, 0],
                        [0, 0, 0, 0, 0, 0,  0,  0, 0,         0,         0,  0, 1]])
        else:
            F = np.mat([[1, 0, dt,  0, 0, 0.5*dt**2,         0,  0, 0],
                        [0, 1,  0, dt, 0,         0, 0.5*dt**2,  0, 0],
                        [0, 0,  1,  0, 0,        dt,         0,  0, 0],
                        [0, 0,  0,  1, 0,         0,        dt,  0, 0],
                        [0, 0,  0,  0, 0,         0,         0,  0, 0],
                        [0, 0,  0,  0, 0,         1,         0,  0, 0],
                        [0, 0,  0,  0, 0,         0,         1,  0, 0],
                        [0, 0,  0,  0, 0,         0,         0,  0, 0],
                        [0, 0,  0,  0, 0,         0,         0,  0, 1]])
        return F

    def getMeaStateH(self) -> np.mat:
        """obtain matrix in the motion_module/script/Linear_kinect_jacobian.ipynb
        """
        if self.has_velo:
            if not self.has_geofilter:
                H = np.mat([[1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]])
            else:
                H = np.mat([[1, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 1, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 1, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 1, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 0, 0, 0, 1]])
        else:
            if not self.has_geofilter:
                H = np.mat([[1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]])
            else:
                H = np.mat([[1, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 1, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 0, 0, 0, 1]])
        return H

    def getOutputInfo(self, state: np.mat) -> np.array:
        """convert state vector in the filter to the output format
        Note that, tra score will be process later
        """
        rotation = Quaternion(axis=(0, 0, 1), radians=state[-1, 0]).q

        if not self.has_geofilter:
            list_state = state.T.tolist()[0][:8] + rotation.tolist()
        else:
            list_state = state.T.tolist()[0][:2] + [-1, -1, -1, -1] + state.T.tolist()[0][2:4] + rotation.tolist()

        return np.array(list_state)

    @staticmethod
    def warpResYawToPi(res: np.mat) -> np.mat:
        """warp res yaw to [-pi, pi) in place

        Args:
            res (np.mat): [measure dim, 1]
            res infos -> [x, y, z, w, l, h, (vx, vy, optional), ry]

        Returns:
            np.mat: [measure dim, 1], residual warped to [-pi, pi)
        """
        res[-1, 0] = warp_to_pi(res[-1, 0])
        return res

    @staticmethod
    def warpStateYawToPi(state: np.mat) -> np.mat:
        """warp state yaw to [-pi, pi) in place

        Args:
            state (np.mat): [state dim, 1]
            State vector: [x, y, z, w, l, h, vx, vy, vz, ax, ay, az, ry]

        Returns:
            np.mat: [state dim, 1], state after warping
        """
        state[-1, 0] = warp_to_pi(state[-1, 0])
        return state

class CTRV(ABC_MODEL):
    """
    Basic info:
        State vector: [x, y, (z, w, l, h, optional) v, ry, ry_rate]
        Measure vector: [x, y, (z, w, l, h, optional), (vx, vy, optional), ry]
    """
    def __init__(self, has_velo: bool, has_geofilter: bool, dt: float) -> None:
        super().__init__()
        self.has_velo,self.has_geofilter, self.dt = has_velo, has_geofilter, dt
        # state vector dim
        self.MD = 9 if self.has_velo else 7
        self.MD = (self.MD - 4) if self.has_geofilter else self.MD

        # measure vector dim
        self.SD = 5 if self.has_geofilter else 9


    def getInitState(self, det_infos: dict) -> np.mat:
        """from detection init tracklet
        Yaw(turn) rate are set to 0. when velociy
        on X/Y-Axis are available, the combined velocity is also set to 0
        """
        init_state = np.zeros(shape=self.SD)
        det, det_box = det_infos['np_array'], det_infos['nusc_box']

        if not self.has_geofilter:
            # set x, y, z, w, l, h, (v, if velo is valid)
            init_state[:6] = det[:6]
            if self.has_velo: init_state[6] = np.hypot(det[6], det[7])
        else:
            init_state[:2] = det[:2]
            if self.has_velo: init_state[2] = np.hypot(det[6], det[7])

        # set yaw
        init_state[-2] = det_box.yaw

        # only for debug
        q = Quaternion(det[8:12].tolist())
        q = -q if q.axis[-1] < 0 else q
        assert q.radians == det_box.yaw

        return np.mat(init_state).T

    def getInitCovP(self, cls_label: int) -> np.mat:
        """init errorcov. In general, when the speed is observable,
        the CTRV model can converge quickly, but when the speed is not measurable,
        we need to carefully set the initial covariance to help the model converge
        """
        if not self.has_velo:
            cls_name = CLASS_STR_TO_SEG_CLASS[cls_label]
            vector_p = CTRV_INIT_EFKP[cls_name] if cls_name in CTRV_INIT_EFKP else CTRV_INIT_EFKP['car']
        else:
            vector_p = CTRV_INIT_EFKP['car']

        if self.has_geofilter:
            vector_p = np.append(vector_p[:2], vector_p[-3:])
        return np.mat(np.diag(vector_p))

    def getProcessNoiseQ(self, cls_label) -> np.mat:
        """set process noise(fix)
        """
        Q = np.mat(np.eye(self.SD)) * FINETUNE_Q[cls_label]
        Q[-2, -2] /= 2
        Q[-3, -3] *= 2
        return Q


    def getMeaNoiseR(self, cls_label) -> np.mat:
        """set measure noise(fix)
        """
        R = np.mat(np.eye(self.MD)) * FINETUNE_R[cls_label]
        R[-1, -1] /= 2
        if self.has_velo:
            R[-2, -2] *= 2
            R[-3, -3] *= 2
        return R

    def stateTransition(self, state: np.mat) -> np.mat:
        """state transition,
        obtain analytical solutions in the motion_module/script/CTRV_kinect_jacobian.ipynb
        Args:
            state (np.mat): [state dim, 1] the estimated state of the previous frame
        Returns:
            np.mat: [state dim, 1] the predict state of the current frame
        """
        assert state.shape == (9, 1) or state.shape == (5, 1), "state vector number in CTRV must equal to 9 or 5"

        dt = self.dt
        if not self.has_geofilter:
            x, y, z, w, l, h, v, theta, omega = state.T.tolist()[0]
        else:
            x, y, v, theta, omega = state.T.tolist()[0]

        yaw_sin, yaw_cos = np.sin(theta), np.cos(theta)
        next_v, next_ry = v, theta + omega * dt

        # corner case(tiny yaw rate), prevent divide-by-zero overflow
        if abs(omega) < 0.001:
            displacement = v * dt
            if not self.has_geofilter:
                predict_state = [x + displacement * yaw_cos,
                                y + displacement * yaw_sin,
                                z, w, l, h,
                                next_v,
                                next_ry, omega]
            else:
                predict_state = [x + displacement * yaw_cos,
                y + displacement * yaw_sin,
                next_v,
                next_ry, omega]
        else:
            ry_rate_inv = 1.0 / omega
            next_yaw_sin, next_yaw_cos = np.sin(next_ry), np.cos(next_ry)
            if not self.has_geofilter:
                predict_state = [x + ry_rate_inv *
                            ( v * next_yaw_sin - v * yaw_sin),
                                y + ry_rate_inv * ( -v * next_yaw_cos + v * yaw_cos),
                                z, w, l, h,
                                next_v,
                                next_ry, omega]
            else:
                predict_state = [x + ry_rate_inv *
                            ( v * next_yaw_sin - v * yaw_sin),
                                y + ry_rate_inv * ( -v * next_yaw_cos + v * yaw_cos),
                                next_v,
                                next_ry, omega]

        return np.mat(predict_state).T

    def StateToMeasure(self, state: np.mat) -> np.mat:
        """get state vector in the measure space
        state vector -> [x, y, z, w, l, h, v, ry, ry_rate]
        measure space -> [x, y, z, w, l, h, (vx, vy, optional), ry]
        Args:
            state (np.mat): [state dim, 1] the predict state of the current frame
        Returns:
            np.mat: [measure dim, 1] state vector projected in the measure space
        """
        assert state.shape == (9, 1) or state.shape == (5, 1), "state vector number in CTRV must equal to 9 or 5"

        if not self.has_geofilter:
            x, y, z, w, l, h, v, theta, _ = state.T.tolist()[0]
        else:
            x, y, v, theta, _ = state.T.tolist()[0]
        if self.has_velo:
            if not self.has_geofilter:
                state_info = [x, y, z,
                            w, l, h,
                            v * np.cos(theta),
                            v * np.sin(theta),
                            theta]
            else:
                state_info = [x, y,
                              v * np.cos(theta),
                              v * np.sin(theta),
                              theta]
        else:
            if not self.has_geofilter:
                state_info = [x, y, z,
                              w, l, h,
                              theta]
            else:
                state_info = [x, y,
                              theta]

        return np.mat(state_info).T

    def getTransitionF(self, state: np.mat) -> np.mat:
        """obtain matrix in the motion_module/script/CTRV_kinect_jacobian.ipynb
        d(stateTransition) / d(state) at previous_state
        """
        dt = self.dt
        if not self.has_geofilter:
            _, _, _, _, _, _, v, theta, omega = state.T.tolist()[0]
        else:
            _, _, v, theta, omega = state.T.tolist()[0]

        yaw_sin, yaw_cos = np.sin(theta), np.cos(theta)

        # corner case, tiny turn rate
        if abs(omega) < 0.001:
            if not self.has_geofilter:
                F = np.mat([[1, 0, 0, 0, 0, 0, dt * yaw_cos, -dt * v * yaw_sin, 0],
                            [0, 1, 0, 0, 0, 0, dt * yaw_sin,  dt * v * yaw_cos, 0],
                            [0, 0, 1, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 1, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 1, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 1, 0, 0, 0],
                            [0, 0, 0, 0, 0, 0, 1, 0, 0],
                            [0, 0, 0, 0, 0, 0, 0, 1, dt],
                            [0, 0, 0, 0, 0, 0, 0, 0, 1]])
            else:
                F = np.mat([[1, 0, dt * yaw_cos, -dt * v * yaw_sin, 0],
                            [0, 1, dt * yaw_sin,  dt * v * yaw_cos, 0],
                            [0, 0, 1, 0, 0],
                            [0, 0, 0, 1, dt],
                            [0, 0, 0, 0, 1]])
        else:
            ry_rate_inv, ry_rate_inv_square = 1 / omega, 1 / (omega * omega)
            next_ry = theta + omega * dt
            next_yaw_sin, next_yaw_cos = np.sin(next_ry), np.cos(next_ry)
            if not self.has_geofilter:
                F = np.mat([[1, 0, 0, 0, 0, 0, -ry_rate_inv * (yaw_sin - next_yaw_sin),
                            ry_rate_inv * (v * next_yaw_cos - v * yaw_cos),
                                ry_rate_inv_square * (v * yaw_sin - v * next_yaw_sin) + ry_rate_inv * dt * v * next_yaw_cos],
                            [0, 1, 0, 0, 0, 0, ry_rate_inv * (yaw_cos - next_yaw_cos),
                            ry_rate_inv * (v * next_yaw_sin - v * yaw_sin),
                                ry_rate_inv_square * (v * next_yaw_cos - v * yaw_cos) + ry_rate_inv * dt * v * next_yaw_sin],
                            [0, 0, 1, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 1, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 1, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 1, 0, 0, 0],
                            [0, 0, 0, 0, 0, 0, 1, 0, 0],
                            [0, 0, 0, 0, 0, 0, 0, 1, dt],
                            [0, 0, 0, 0, 0, 0, 0, 0, 1]])
            else:
                F = np.mat([[1, 0, -ry_rate_inv * (yaw_sin - next_yaw_sin),
                            ry_rate_inv * (v * next_yaw_cos - v * yaw_cos),
                                ry_rate_inv_square * (v * yaw_sin - v * next_yaw_sin) + ry_rate_inv * dt * v * next_yaw_cos],
                            [0, 1, ry_rate_inv * (yaw_cos - next_yaw_cos),
                            ry_rate_inv * (v * next_yaw_sin - v * yaw_sin),
                                ry_rate_inv_square * (v * next_yaw_cos - v * yaw_cos) + ry_rate_inv * dt * v * next_yaw_sin],
                            [0, 0, 1, 0, 0],
                            [0, 0, 0, 1, dt],
                            [0, 0, 0, 0, 1]])

        return F

    def getMeaStateH(self, state: np.mat) -> np.mat:
        """obtain matrix in the motion_module/script/CTRV_kinect_jacobian.ipynb
        d(StateToMeasure) / d(state) at predict_state
        """

        if self.has_velo:
            if not self.has_geofilter:
                _, _, _, _, _, _, v, theta, _ = state.T.tolist()[0]
                yaw_sin, yaw_cos = np.sin(theta), np.cos(theta)
                H = np.mat([[1, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 1, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 1, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 1, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 1, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 1, 0, 0, 0],
                            [0, 0, 0, 0, 0, 0, yaw_cos, -v * yaw_sin, 0],
                            [0, 0, 0, 0, 0, 0, yaw_sin, v * yaw_cos, 0],
                            [0, 0, 0, 0, 0, 0, 0, 1, 0]])
            else:
                _, _, v, theta, _ = state.T.tolist()[0]
                yaw_sin, yaw_cos = np.sin(theta), np.cos(theta)
                H = np.mat([[1, 0, 0, 0, 0],
                            [0, 1, 0, 0, 0],
                            [0, 0, yaw_cos, -v * yaw_sin, 0],
                            [0, 0, yaw_sin, v * yaw_cos, 0],
                            [0, 0, 0, 1, 0]])
        else:
            if not self.has_geofilter:
                H = np.mat([[1, 0, 0, 0, 0, 0, 0, 0, 0],
                            [0, 1, 0, 0, 0, 0, 0, 0, 0],
                            [0, 0, 1, 0, 0, 0, 0, 0, 0],
                            [0, 0, 0, 1, 0, 0, 0, 0, 0],
                            [0, 0, 0, 0, 1, 0, 0, 0, 0],
                            [0, 0, 0, 0, 0, 1, 0, 0, 0],
                            [0, 0, 0, 0, 0, 0, 0, 1, 0]])
            else:
                H = np.mat([[1, 0, 0, 0, 0],
                            [0, 1, 0, 0, 0],
                            [0, 0, 0, 1, 0]])
        return H

    def getOutputInfo(self, state: np.mat) -> np.array:
        """convert state vector in the filter to the output format
        Note that, tra score will be process later
        """

        rotation = Quaternion(axis=(0, 0, 1), radians=state[-2, 0]).q
        if not self.has_geofilter:
            v, yaw = state[-3, 0], state[-2, 0]
            list_state = state.T.tolist()[0][:6] + [v * np.cos(yaw), v * np.sin(yaw)] + rotation.tolist()
        else:
            v, yaw = state[-3, 0],state[-2, 0]
            list_state = state.T.tolist()[0][:2] + [-1, -1, -1, -1] + [v * np.cos(yaw), v * np.sin(yaw)] + rotation.tolist()
        return np.array(list_state)


    @staticmethod
    def warpResYawToPi(res: np.mat) -> np.mat:
        """warp res yaw to [-pi, pi) in place
        Args:
            res (np.mat): [measure dim, 1]
            res infos -> [x, y, z, w, l, h, (vx, vy, optional), ry]
        Returns:
            np.mat: [measure dim, 1], residual warped to [-pi, pi)
        """
        res[-1, 0] = warp_to_pi(res[-1, 0])
        return res

    @staticmethod
    def warpStateYawToPi(state: np.mat) -> np.mat:
        """warp state yaw to [-pi, pi) in place
        Args:
            state (np.mat): [state dim, 1]
            State vector: [x, y, z, w, l, h, v, ry, ry_rate]
        Returns:
            np.mat: [state dim, 1], state after warping
        """
        state[-2, 0] = warp_to_pi(state[-2, 0])
        return state