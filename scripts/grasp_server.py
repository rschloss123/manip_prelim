#! /usr/bin/env python
import rospy
import actionlib
import tf
import tf2_ros
from tf import TransformListener
from std_msgs.msg import *
from geometry_msgs.msg import *
from hsrb_interface import Robot, exceptions, geometry
from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal


from manip_prelim.msg import *
import math

import numpy as np 
from numpy import linalg as LA
from numpy.linalg import inv


_BASE_TF = 'base_link'
_MAP_TF = 'map'
_HEAD_TF = 'head_rgbd_sensor_link'
_ARM_LIFT_TF = 'arm_lift_link'
_ORIGIN_TF = 'head_rgbd_sensor_link' 
ARM_LENGTH = 0.35; # distance from shoulder joint to wrist joints. measured on the robot

class GraspAction(object):

	def __init__(self, robot):

		self.robot = robot 
		self.gripper_state=True
		self.print_count=0

		self.target_pose=PoseStamped()

		# in the base link frame
		self.target_backup=PoseStamped()
		self.target_backup.pose.position.x=-0.1
		self.target_backup.pose.position.y=0.0
		self.target_backup.pose.position.z=0.0
		self.target_backup.pose.orientation.x=0.0
		self.target_backup.pose.orientation.y=0.0
		self.target_backup.pose.orientation.z=0.0
		self.target_backup.pose.orientation.w=0.0
		self.target_backup.header.frame_id=_BASE_TF


		self.listener=tf.TransformListener()
		# # transform from base frame to map frame
		self.listener.waitForTransform(_BASE_TF,_MAP_TF,rospy.Time(),rospy.Duration(2.0))

		# ---------------------------------------------------------------------

		t = self.listener.getLatestCommonTime(_BASE_TF, _MAP_TF)
		position, quaternion = self.listener.lookupTransform(_BASE_TF, _MAP_TF, t)
		print "map wrt to base"
		print "position", position
		print " "
		print "quaternion", quaternion
		print " "

		e = tf.transformations.euler_from_quaternion(quaternion)
		R_map_wrt_base = tf.transformations.euler_matrix(e[0], e[1], e[2], 'rxyz')
		self.R_map_wrt_base = R_map_wrt_base[:3,:3]

		print "self.R_map_wrt_base"
		print self.R_map_wrt_base 

		self.x_align = True if np.abs(self.R_map_wrt_base[0,0]) >= np.abs(self.R_map_wrt_base[1,0]) else False  
		print np.abs(self.R_map_wrt_base[0,0])
		print np.abs(self.R_map_wrt_base[1,0]) 
		print self.x_align 

		# # t2 = listener.getLatestCommonTime(_HEAD_TF, _MAP_TF)
		# # position, quaternion = listener.lookupTransform(_HEAD_TF, _MAP_TF, t2)
		# # print "map wrt to head"
		# # print "position", position
		# # print " "
		# # print "quaternion", quaternion
		

		# exit()
		# ----------------------------------------------------------------------


		self.error_threshold = 0.01

		self.vel_pub = rospy.Publisher('/hsrb/command_velocity', geometry_msgs.msg.Twist, queue_size=10)

		global_pose_topic = 'global_pose'
		self.global_pose_sub = rospy.Subscriber(global_pose_topic, PoseStamped, self.pose_callback)

		while not rospy.is_shutdown():
			try:
				self.body=self.robot.try_get('whole_body')
				self.gripper = self.robot.try_get('gripper')
				# self.base=self.robot.try_get('omni_base')
				self.open_gripper()
				self.body.move_to_neutral()
				break 
			except(exceptions.ResourceNotFoundError, exceptions.RobotConnectionError) as e:
				rospy.logerr("Failed to obtain resource: {}\nRetrying...".format(e))

		# navigation client to move the base 
		self.navi_cli = actionlib.SimpleActionClient('/move_base/move', MoveBaseAction)

		self._as = actionlib.SimpleActionServer('pickUpaction', manip_prelim.msg.pickUpAction, execute_cb=self.pickUp, auto_start=False)
		self._as.start()

	def compute_error(self, target):
		# target location
		target_x = target.pose.position.x 
		target_y = target.pose.position.y 
		target_z = target.pose.position.z
		target_rx = target.pose.orientation.x
		target_ry = target.pose.orientation.y
		target_rz = target.pose.orientation.z
		target_rw = target.pose.orientation.w

		# current location
		curr_x = self.robot_pos.position.x 
		curr_y = self.robot_pos.position.y
		curr_z = self.robot_pos.position.z
		curr_rx = self.robot_pos.orientation.x  
		curr_ry = self.robot_pos.orientation.y
		curr_rz = self.robot_pos.orientation.z
		curr_rw = self.robot_pos.orientation.w   

		error_x = LA.norm(target_x-curr_x) #+ LA.norm(target_y-curr_y) + LA.norm(target_z-curr_z) + LA.norm(target_rx - curr_rx) + LA.norm(target_ry-curr_ry) + LA.norm(target_rz - curr_rz) + LA.norm(target_rw - curr_rw)		
		error_y = LA.norm(target_y-curr_y)

		if self.print_count == 10000: 
			print "error_x", error_x 
			print "error_y", error_y 
			print "target_x", target_x
			print "curr_x",curr_x
			print "target_y", target_y
			print "curr_y",curr_y
			print ""
			self.print_count = 0 
		else: 
			self.print_count+=1 
		
		return error_x, error_y  


	def get_vel_command(self, error, prev_error, step_one, sign): 

		fast_v = .1
		slow_v = .05

		if error > prev_error and step_one == False:
			sign = -sign

		if error > self.error_threshold:
			if error > self.error_threshold+.01:
				v = fast_v
			else:
				v = slow_v
		# else:
			# v = 0.0

		vel_command = sign * v

		return vel_command 		


	def track_motion(self, target_map, backup=False):
		sign_x = 1.0
		sign_y = 1.0

		if backup == True :
			sign_x = -sign_x
		if backup == True :
			sign_y = -sign_y


		step_one = True 
		error_x, error_y  = self.compute_error(target_map)
		original_error_x = error_x 
		original_error_y = error_y 
		prev_error_x = error_x 
		prev_error_y = error_y 

		print "original error x, y", original_error_x, original_error_y
		

		while(error_x > self.error_threshold or error_y > self.error_threshold): # or error_y > self.error_threshold:  
			# TODO consider direction of errors
			# TODO consider twist errors?  
			tw = geometry_msgs.msg.Twist()
			tw.linear.x = 0
			tw.linear.y = 0

			# tw.linear.x = sign_x * v
			if error_x > self.error_threshold:
				if self.x_align == True: 
					tw.linear.x = self.get_vel_command(error_x, prev_error_x, step_one, sign_x)
				else: 
					tw.linear.y = self.get_vel_command(error_x, prev_error_x, step_one, sign_x)
			if error_y > self.error_threshold:
				if self.x_align == True: 
					tw.linear.y = self.get_vel_command(error_y, prev_error_y, step_one, sign_y)
				else: 
					tw.linear.x = self.get_vel_command(error_y, prev_error_y, step_one, sign_y)

			prev_error_x = error_x 
			prev_error_y = error_y 			

			# the velocity 
			self.vel_pub.publish(tw)

			if self.print_count == 10000: 
				print "tw.linear.x", tw.linear.x
				print "tw.linear.y", tw.linear.y 



			error_x, error_y = self.compute_error(target_map)
			step_one = True 

		return 		

	# TODO: figure out assumptions. Will the robot be directly in front of the object?
	def pickUp(self, goal):

		self.target_pose=goal.target_pose

		self.listener.waitForTransform(_ORIGIN_TF,_MAP_TF,rospy.Time(),rospy.Duration(2.0))
		target_pose_map = self.listener.transformPose(_MAP_TF,self.target_pose)
		self.listener.waitForTransform(_ORIGIN_TF,_ARM_LIFT_TF,rospy.Time(),rospy.Duration(2.0))
		target_pose_arm_lift = self.listener.transformPose(_ARM_LIFT_TF,self.target_pose)
		# transform from base frame to map frame

		print "target_pose_map"
		print target_pose_map
		
		# self.listener.waitForTransform(_ORIGIN_TF,_BASE_TF,rospy.Time(),rospy.Duration(2.0))
		# self.target_pose_base = listener.transformPose(_BASE_TF,self.target_pose)

		# make sure gripper is open
		self.open_gripper()
		self.body.move_to_neutral()
		self.gripper_state=True
		rospy.loginfo("open_gripper")

		# calculate transform for arm
		# TODO : add arm height to action and do transformation
		# obj_arm_lift_link = 0.2
		obj_arm_lift_link = target_pose_arm_lift.pose.position.z
		obj_arm_flex_joint = -1.57
		
		self.body.move_to_joint_positions({'arm_flex_joint': obj_arm_flex_joint, 'wrist_flex_joint': 0.0, 'arm_lift_joint':obj_arm_lift_link})

		print "obj_arm_lift_link",obj_arm_lift_link

		self.track_motion(target_pose_map,backup=False)

		print "forward motion complete"
		
		rospy.sleep(2.0)
		self.close_gripper()
		self.gripper_state=False
		rospy.loginfo("close_gripper")

		self.listener.waitForTransform(_BASE_TF,_MAP_TF,rospy.Time(),rospy.Duration(2.0))
		# target position to back up to in the map frame 
		self.target_backup_map = self.listener.transformPose(_MAP_TF,self.target_backup)

		self.track_motion(self.target_backup_map,backup=True)

		rospy.loginfo("back up complete")

		self.body.move_to_neutral()

		rospy.loginfo("close gripper")
		

		self._as.set_succeeded()

	# TODO
	def setDown():

		self.open_gripper()

	def pose_callback(self,msg):
		self.robot_pos=msg.pose 

	def open_gripper(self,to_width=1.2):
		self.gripper.command(to_width)

	def close_gripper(self, to_width=-0.01):
		# self.gripper.grasp(to_width)
		self.gripper.apply_force(1.0)

	# def navigation_action(goal_x,goal_y,goal_yaw):
	# 	pose = PoseStamped()
	# 	pose.header.stamp = rospy.Time.now()
	# 	pose.header.frame_id = "map"
	# 	pose.pose.position = Point(goal_x, goal_y, 0)
	# 	quat = tf.transformations.quaternion_from_euler(0, 0, goal_yaw)
	# 	pose.pose.orientation = Quaternion(*quat)

	# 	goal = MoveBaseGoal()
	# 	goal.target_pose = pose

	# 	# send message to the action server
	# 	self.navi_cli.send_goal(goal)

	# 	# wait for the action server to complete the order
	# 	self.navi_cli.wait_for_result()

	# 	# print result of navigation
	# 	result_action_state = self.navi_cli.get_state()

	# 	return #result_action_state 

if __name__ == '__main__':
	robot = Robot()
	rospy.loginfo("Initializing givepose server")
	server=GraspAction(robot)
	rospy.loginfo("grasp_action_server created")
	rospy.spin()

