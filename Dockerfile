FROM artifactory.aws.wiley.com/docker/amazonlinux:2023 as clamav

ARG clamav_version=1.4.1

RUN yum update -y
RUN yum install -y cpio wget

# Set up working directories
RUN mkdir -p /opt/app/bin/

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

FROM artifactory.aws.wiley.com/docker/amazonlinux:2023 as lambda

ARG dist=/tmp/av

# Install packages
RUN yum update -y
RUN yum install -y python3-pip yum-utils less

# Copy in the lambda source
RUN mkdir -p $dist
COPY ./*.py $dist/
COPY requirements.txt $dist/requirements.txt

# This had --no-cache-dir, tracing through multiple tickets led to a problem in wheel
WORKDIR $dist
RUN pip3 install -r requirements.txt

COPY /usr/local/lib64/python3.9/site-packages/ $dist/
COPY /usr/local/lib/python3.9/site-packages/ $dist/

FROM artifactory.aws.wiley.com/docker/amazonlinux:2023

# Install packages
RUN yum update -y
RUN yum install -y zip

COPY --from=clamav /opt/app/bin /opt/app/bin
COPY --from=lambda /tmp/av /opt/app

# Create the zip file
WORKDIR /opt/app
RUN zip -r9 --exclude="*test*" /opt/app/build/lambda.zip *.py bin

WORKDIR /opt/app
