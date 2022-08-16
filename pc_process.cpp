#include<iostream>
#include<string>
// #include <ros/ros.h>
// #include <sensor_msgs/point_cloud_conversion.h>
// #include <sensor_msgs/PointCloud2.h>

using namespace std;
// using namespace pcl;

extern "C"{
void pc(float n){
    // ros::NodeHandle nh;
    // ros::Subscriber sub = nh.subscribe ("none_ground_topic", 160, cloud_cb);  //订阅者  none_ground_topic -- 话题topic名
    
    cout << typeid(n).name() << endl;
}
}