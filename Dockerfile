FROM artifactory.aws.wiley.com/docker/amazonlinux:2023 as clamav

ARG clamav_version=1.4.1

RUN yum update -y
RUN yum install -y cpio wget

# Set up working directories
RUN mkdir -p /opt/app/bin/
RUN mkdir -p /opt/app/python_modules

# Download libraries we need to run in lambda
WORKDIR /tmp
RUN wget https://www.clamav.net/downloads/production/clamav-${clamav_version}.linux.x86_64.deb -O clamav.deb -U "User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:93.0) Gecko/20100101 Firefox/93.0" --no-verbose

RUN dpkg-deb -R clamav.deb /tmp

# Copy over the binaries and libraries
RUN cp -r /tmp/usr/local/bin/clamdscan \
       /tmp/usr/local/sbin/clamd \
       /tmp/usr/local/bin/freshclam \
       /tmp/usr/local/lib/lib* \
       /opt/app/bin/

RUN echo "DatabaseDirectory /tmp/clamav_defs" > /opt/app/bin/scan.conf
RUN echo "PidFile /tmp/clamd.pid" >> /opt/app/bin/scan.conf
RUN echo "LogFile /tmp/clamd.log" >> /opt/app/bin/scan.conf
RUN echo "LocalSocket /tmp/clamd.sock" >> /opt/app/bin/scan.conf
RUN echo "FixStaleSocket yes" >> /opt/app/bin/scan.conf

# Fix the freshclam.conf settings
RUN echo "DatabaseMirror database.clamav.net" > /opt/app/bin/freshclam.conf
RUN echo "CompressLocalDatabase yes" >> /opt/app/bin/freshclam.conf

FROM artifactory.aws.wiley.com/docker/python:3.12

ARG dist=/opt/app

# Install packages
RUN apt-get update; \
    apt-get install -y --no-install-recommends \
      zip

# Copy in the lambda source
RUN mkdir -p $dist/build
COPY --from=clamav /opt/app/bin /opt/app/bin
COPY ./*.py $dist/
COPY requirements.txt $dist/requirements.txt

# This had --no-cache-dir, tracing through multiple tickets led to a problem in wheel
WORKDIR $dist
RUN pip install -r requirements.txt
RUN rm -rf /root/.cache/pip

# Create the zip file
RUN zip -r9 --exclude="*test*" $dist/build/lambda.zip *.py bin

WORKDIR /usr/local/lib/python3.12/site-packages
RUN zip -r9 $dist/build/lambda.zip *

WORKDIR $dist
