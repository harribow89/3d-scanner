# ROS 2 Jazzy + RTAB-Map + OpenNI2 (Asus Xtion) for the 3D scanner.
# Built on Kali via Docker because Kali is not a supported ROS apt target.
FROM ros:jazzy-ros-base

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ros-jazzy-rtabmap-ros \
      ros-jazzy-rtabmap-launch \
      ros-jazzy-rtabmap \
      ros-jazzy-openni2-camera \
      ros-jazzy-rviz2 \
      ros-jazzy-rtabmap-rviz-plugins \
      openni2-utils \
      libopenni2-0 \
    && rm -rf /var/lib/apt/lists/*

# Source ROS in every interactive shell.
RUN echo "source /opt/ros/jazzy/setup.bash" >> /root/.bashrc

WORKDIR /scanner
