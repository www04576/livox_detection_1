import os
import numpy as np
import tensorflow as tf
import copy
import config.config as cfg
from networks.model import *
import lib_cpp

import time

import rospy
import message_filters
import std_msgs.msg
from geometry_msgs.msg import Point
from sensor_msgs.msg import PointCloud2
from geometry_msgs.msg import Point32
from geometry_msgs.msg import Quaternion
import sensor_msgs.point_cloud2 as pcl2
from visualization_msgs.msg import Marker
from visualization_msgs.msg import MarkerArray

import numba 
from numba import jit

import ctypes
import ros_numpy


mnum = 0
marker_array = MarkerArray()
marker_array_text = MarkerArray()

DX = cfg.VOXEL_SIZE[0]
DY = cfg.VOXEL_SIZE[1]
DZ = cfg.VOXEL_SIZE[2]

X_MIN = cfg.RANGE['X_MIN']
X_MAX = cfg.RANGE['X_MAX']

Y_MIN = cfg.RANGE['Y_MIN']
Y_MAX = cfg.RANGE['Y_MAX']

Z_MIN = cfg.RANGE['Z_MIN']
Z_MAX = cfg.RANGE['Z_MAX']

overlap = cfg.OVERLAP
HEIGHT = round((X_MAX - X_MIN+2*overlap) / DX)
WIDTH = round((Y_MAX - Y_MIN) / DY)
CHANNELS = round((Z_MAX - Z_MIN) / DZ)

GROUND_FIX = 1


print(HEIGHT, WIDTH, CHANNELS)

T1 = np.array([[0.0, -1.0, 0.0, 0.0],
               [0.0, 0.0, -1.0, 0.0],
               [1.0, 0.0, 0.0, 0.0],
               [0.0, 0.0, 0.0, 1.0]]
              )
lines = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6],
         [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]]


class Detector(object):
    def __init__(self, *, nms_threshold=0.1, weight_file=None):
        self.net = livox_model(HEIGHT, WIDTH, CHANNELS)
        with tf.Graph().as_default():
            with tf.device('/gpu:'+str(cfg.GPU_INDEX)):
                input_bev_img_pl = \
                    self.net.placeholder_inputs(cfg.BATCH_SIZE)
                end_points = self.net.get_model(input_bev_img_pl)

                saver = tf.compat.v1.train.Saver()
                config = tf.compat.v1.ConfigProto()
                config.gpu_options.allow_growth = True
                config.allow_soft_placement = True
                config.log_device_placement = False
                self.sess = tf.compat.v1.Session(config=config)
                saver.restore(self.sess, cfg.MODEL_PATH)
                self.ops = {'input_bev_img_pl': input_bev_img_pl,  # input
                            'end_points': end_points,  # output
                            }
        rospy.init_node('livox_test', anonymous=True)


        # self.sub = rospy.Subscriber(
        #     "/livox/lidar", PointCloud2, queue_size=1, callback=self.LivoxCallback)

        self.livox1 = message_filters.Subscriber("/livox/lidar_3WEDJC6001Z2681", PointCloud2, queue_size=1)
        self.livox2 = message_filters.Subscriber("/livox/lidar_3WEDK2K001T0731", PointCloud2, queue_size=1)

        livox_sync = message_filters.ApproximateTimeSynchronizer([self.livox1, self.livox2],10,0.1,allow_headerless=True)
        livox_sync.registerCallback(self.LivoxCallback)


        self.marker_pub = rospy.Publisher(
            '/detect_box3d', MarkerArray, queue_size=1)
        self.marker_text_pub = rospy.Publisher(
            '/text_det', MarkerArray, queue_size=1)
        # self.pointcloud_pub = rospy.Publisher(
        #     '/pointcloud', PointCloud2, queue_size=1)

    def roty(self, t):
        c = np.cos(t)
        s = np.sin(t)
        return np.array([[c,  0,  s],
                         [0,  1,  0],
                         [-s, 0,  c]])

    def get_3d_box(self, box_size, heading_angle, center):
        ''' Calculate 3D bounding box corners from its parameterization.

        Input:
            box_size: tuple of (l,w,h)
            heading_angle: rad scalar, clockwise from pos x axis
            center: tuple of (x,y,z)
        Output:
            corners_3d: numpy array of shape (8,3) for 3D box cornders
        '''
        R = self.roty(heading_angle)
        l, w, h = box_size
        x_corners = [l/2, l/2, -l/2, -l/2, l/2, l/2, -l/2, -l/2]
        y_corners = [h/2, h/2, h/2, h/2, -h/2, -h/2, -h/2, -h/2]
        z_corners = [w/2, -w/2, -w/2, w/2, w/2, -w/2, -w/2, w/2]
        corners_3d = np.dot(R, np.vstack([x_corners, y_corners, z_corners]))
        corners_3d[0, :] = corners_3d[0, :] + center[0]
        corners_3d[1, :] = corners_3d[1, :] + center[1]
        corners_3d[2, :] = corners_3d[2, :] + center[2]
        corners_3d = np.transpose(corners_3d)
        return corners_3d



    def detect(self, batch_bev_img):
        feed_dict = {self.ops['input_bev_img_pl']: batch_bev_img}
        feature_out,\
            = self.sess.run([self.ops['end_points']['feature_out'],
                             ], feed_dict=feed_dict)
        result = lib_cpp.cal_result(feature_out[0,:,:,:], \
                                    cfg.BOX_THRESHOLD,overlap,X_MIN,HEIGHT, WIDTH, cfg.VOXEL_SIZE[0], cfg.VOXEL_SIZE[1], cfg.VOXEL_SIZE[2], cfg.NMS_THRESHOLD)
        is_obj_list = result[:, 0].tolist()
        
        reg_m_x_list = result[:, 5].tolist()
        reg_w_list = result[:, 4].tolist()
        reg_l_list = result[:, 3].tolist()
        obj_cls_list = result[:, 1].tolist()
        reg_m_y_list = result[:, 6].tolist()
        reg_theta_list = result[:, 2].tolist()
        reg_m_z_list = (result[:, 8] - GROUND_FIX).tolist()
        reg_h_list = result[:, 7].tolist()

        results = []
        for i in range(len(is_obj_list)):
            box3d_pts_3d = np.ones((8, 4), float)
            box3d_pts_3d[:, 0:3] = self.get_3d_box( \
                (reg_l_list[i], reg_w_list[i], reg_h_list[i]), \
                reg_theta_list[i], (reg_m_x_list[i], reg_m_z_list[i], reg_m_y_list[i]))
            box3d_pts_3d = np.dot(np.linalg.inv(T1), box3d_pts_3d.T).T  # n*4
            if int(obj_cls_list[i]) == 0:
                cls_name = "car"
            elif int(obj_cls_list[i]) == 1:
                cls_name = "bus"
            elif int(obj_cls_list[i]) == 2:
                cls_name = "truck"
            elif int(obj_cls_list[i]) == 3:
                cls_name = "pedestrian"
            else:
                cls_name = "bimo"
            results.append([cls_name,
                            box3d_pts_3d[0][0], box3d_pts_3d[1][0], box3d_pts_3d[2][0], box3d_pts_3d[3][0],
                            box3d_pts_3d[4][0], box3d_pts_3d[5][0], box3d_pts_3d[6][0], box3d_pts_3d[7][0],
                            box3d_pts_3d[0][1], box3d_pts_3d[1][1], box3d_pts_3d[2][1], box3d_pts_3d[3][1],
                            box3d_pts_3d[4][1], box3d_pts_3d[5][1], box3d_pts_3d[6][1], box3d_pts_3d[7][1],
                            box3d_pts_3d[0][2], box3d_pts_3d[1][2], box3d_pts_3d[2][2], box3d_pts_3d[3][2],
                            box3d_pts_3d[4][2], box3d_pts_3d[5][2], box3d_pts_3d[6][2], box3d_pts_3d[7][2],
                            is_obj_list[i]])
        return results

    def LivoxMix(self, livox1, livox2, livox3 = None, livox4 = None, livox5 = None, livox6 = None):
        pass

    def LivoxCallback(self, livox1 = None, livox2 = None, livox3 = None, livox4 = None, livox5 = None, livox6 = None):
        global mnum
        header = std_msgs.msg.Header()
        header.stamp = rospy.Time.now()
        header.frame_id = 'livox_frame'
        points_list = []
        t0 = time.time()
        # for point in pcl2.read_points(msg, skip_nans=True, field_names=("x", "y", "z", "intensity")):
        #     #if point[0] == 0 and point[1] == 0 and point[2] == 0:
        #     #    continue
        #     if np.abs(point[0]) < 2.0 and np.abs(point[1]) < 1.5:
        #         continue
        #     if np.abs(point[0]) > X_MAX or \
        #         np.abs(point[1]) > Y_MAX or \
        #         np.abs(point[2]) > Z_MAX:
        #         continue
        #     points_list.append(point)
        
        # points_list = pcl2.read_points(msg, skip_nans=True, field_names=("x", "y", "z", "intensity"))
        
        # ll = ctypes.cdll.LoadLibrary
        # lib = ll('./pc_process.so')
        # lib.pc(points_list)

        for msg in [livox1, livox2, livox3, livox4, livox5, livox6]:
            if msg != None:
                cur_points = ros_numpy.numpify(msg)
                points_list = np.stack(cur_points[f] for f in ('x', 'y', 'z')).T

        # points_list = np.asarray(points_list[0: 3], dtype=np.float32)
        # points_list = list(points_list)
        # t0_5 = time.time()
        # print('point_processing_time_1(ms)', 1000*(t0_5-t0))
        # points_list = np.asarray(points_list, dtype=np.float32) #??????np.array??????????????????
        # pointcloud_msg = pcl2.create_cloud_xyz32(header, points_list[:, 0:3])
        t1 = time.time()
        print('\npoint_processing_time(ms)', 1000*(t1-t0))
        vox = data2voxel(points_list)
        # t1_5 = time.time()
        # print('voxelization_time_1(ms)', 1000*(t1_5-t1))
        vox = np.expand_dims(vox, axis=0)
        t2 = time.time()
        print('voxelization_time(ms)', 1000*(t2-t1))
        result = self.detect(vox)
        t3 = time.time()
        print('det_time(ms)', 1000*(t3-t2))
        print('det_numbers', len(result))
        for ii in range(len(result)):
            result[ii][1:9] = list(np.array(result[ii][1:9]))
            result[ii][9:17] = list(np.array(result[ii][9:17]))
            result[ii][17:25] = list(np.array(result[ii][17:25]))
        boxes = result
        marker_array.markers.clear()
        marker_array_text.markers.clear()
        for obid in range(len(boxes)):
            ob = boxes[obid]
            # tid = 0
            detect_points_set = []
            for i in range(0, 8):
                detect_points_set.append(Point(ob[i+1], ob[i+9], ob[i+17]))

            marker = Marker()
            marker.header.frame_id = 'livox_frame'
            marker.header.stamp = rospy.Time.now()

            marker.id = obid*2
            marker.action = Marker.ADD
            marker.type = Marker.LINE_LIST

            marker.lifetime = rospy.Duration(0)

            marker.color.r = 1
            marker.color.g = 0
            marker.color.b = 0

            marker.color.a = 1
            marker.scale.x = 0.2
            marker.points = []

            for line in lines:
                marker.points.append(detect_points_set[line[0]])
                marker.points.append(detect_points_set[line[1]])

            marker_array.markers.append(marker)
            marker1 = Marker()
            marker1.header.frame_id = 'livox_frame'
            marker1.header.stamp = rospy.Time.now()
            marker1.ns = "basic_shapes"

            marker1.id = obid*2+1
            marker1.action = Marker.ADD

            marker1.type = Marker.TEXT_VIEW_FACING

            marker1.lifetime = rospy.Duration(0)

            marker1.color.r = 1  # cr
            marker1.color.g = 1  # cg
            marker1.color.b = 1  # cb

            marker1.color.a = 1
            marker1.scale.z = 1

            marker1.pose.orientation.w = 1.0
            marker1.pose.position.x = (ob[1]+ob[3])/2
            marker1.pose.position.y = (ob[9]+ob[11])/2
            marker1.pose.position.z = (ob[21]+ob[23])/2+1

            marker1.text = ob[0]+':'+str(np.floor(ob[25]*100)/100)

            marker_array_text.markers.append(marker1)
        if mnum > len(boxes):
            for obid in range(len(boxes), mnum):
                marker = Marker()
                marker.header.frame_id = 'livox_frame'
                marker.header.stamp = rospy.Time.now()
                marker.id = obid*2
                marker.action = Marker.ADD
                marker.type = Marker.LINE_LIST
                marker.lifetime = rospy.Duration(0.01)
                marker.color.r = 1
                marker.color.g = 1
                marker.color.b = 1
                marker.color.a = 0
                marker.scale.x = 0.2
                marker.points = []
                marker_array.markers.append(marker)

                marker1 = Marker()
                marker1.header.frame_id = 'livox_frame'
                marker1.header.stamp = rospy.Time.now()
                marker1.ns = "basic_shapes"

                marker1.id = obid*2+1
                marker1.action = Marker.ADD

                marker1.type = Marker.TEXT_VIEW_FACING

                marker1.lifetime = rospy.Duration(0.01)
                marker1.color.a = 0
                marker1.text = 'aaa'
                marker_array_text.markers.append(marker1)
        mnum = len(boxes)
        self.marker_pub.publish(marker_array)
        # self.pointcloud_pub.publish(pointcloud_msg)
        self.marker_text_pub.publish(marker_array_text)
        toc = time.time()
        print('=========total_time(ms)', 1000*(toc-t0), '=============')


@jit(nopython=True)
def data2voxel(pclist):
    
    # tic0 = time.time()
    
    # data = [0 for i in range(HEIGHT*WIDTH*CHANNELS)]
    data = np.zeros((HEIGHT, WIDTH, CHANNELS))

    # pclist = list(pclist)
    # pclist = np.asarray(pclist, dtype=np.float32)
    # tic1 = time.time()
    # print("vox time 1(ms): ", 1000 * (tic1 - tic0))
    for line in pclist:
        X = line[0]
        Y = line[1]
        if abs(X)<3 and abs(Y)<3:
            continue
        Z = line[2] - GROUND_FIX
        if( Y > Y_MIN and Y < Y_MAX and
            X > X_MIN and X < X_MAX and
            Z > Z_MIN and Z < Z_MAX):
            channel = int((-Z + Z_MAX)/DZ)
            if (X > -overlap):
                pixel_x = int((X - X_MIN + 2*overlap)/DX)
                pixel_y = int((-Y + Y_MAX)/DY)
                data[pixel_x, pixel_y, channel] = 1
            if (X < overlap):
                pixel_x = int((-X + overlap)/DX)
                pixel_y = int((Y + Y_MAX)/DY)
                # data[pixel_x*WIDTH*CHANNELS+pixel_y*CHANNELS+channel] = 1
                data[pixel_x, pixel_y, channel] = 1
    
    # tic2 = time.time()
    # print("vox time 2(ms): ", 1000 * (tic2 - tic1))

    # # voxel = np.reshape(data, (HEIGHT, WIDTH, CHANNELS))
    # toc = time.time()
    # print("vox time 3(ms): ", 1000 * (toc - tic2))
    return data


if __name__ == '__main__':
    livox = Detector()
    
    rospy.spin()
