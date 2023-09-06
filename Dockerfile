FROM amazonlinux:2

ARG clamav_version=1.0.3

# Set up working directories
RUN mkdir -p /opt/app
RUN mkdir -p /opt/app/build
RUN mkdir -p /opt/app/bin/

# Copy in the lambda source
WORKDIR /opt/app
COPY ./*.py /opt/app/
COPY requirements.txt /opt/app/requirements.txt

# Install packages
RUN yum update -y
RUN yum install -y cpio python3-pip yum-utils zip unzip less wget

# This had --no-cache-dir, tracing through multiple tickets led to a problem in wheel
RUN pip3 install -r requirements.txt
RUN rm -rf /root/.cache/pip

# Download libraries we need to run in lambda
WORKDIR /tmp
RUN wget https://www.clamav.net/downloads/production/clamav-${clamav_version}.linux.x86_64.rpm -O clamav.rpm -U "User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:93.0) Gecko/20100101 Firefox/93.0" --no-verbose

RUN rpm2cpio clamav.rpm | cpio -idmv

# Copy over the binaries and libraries
RUN cp -r /tmp/usr/local/bin/clamdscan \
       /tmp/usr/local/sbin/clamd \
       /tmp/usr/local/bin/freshclam \
       /tmp/usr/local/lib64/* \
       /opt/app/bin/

RUN echo "DatabaseDirectory /tmp/clamav_defs" > /opt/app/bin/scan.conf
RUN echo "PidFile /tmp/clamd.pid" >> /opt/app/bin/scan.conf
RUN echo "LogFile /tmp/clamd.log" >> /opt/app/bin/scan.conf
RUN echo "LocalSocket /tmp/clamd.sock" >> /opt/app/bin/scan.conf
RUN echo "FixStaleSocket yes" >> /opt/app/bin/scan.conf

# Fix the freshclam.conf settings
RUN echo "DatabaseMirror database.clamav.net" > /opt/app/bin/freshclam.conf
RUN echo "CompressLocalDatabase yes" >> /opt/app/bin/freshclam.conf

# Create the zip file
WORKDIR /opt/app
RUN zip -r9 --exclude="*test*" /opt/app/build/lambda.zip *.py bin

WORKDIR /usr/local/lib/python3.7/site-packages
RUN zip -r9 /opt/app/build/lambda.zip *
WORKDIR /usr/local/lib64/python3.7/site-packages
RUN zip -r9 /opt/app/build/lambda.zip *

WORKDIR /opt/app
