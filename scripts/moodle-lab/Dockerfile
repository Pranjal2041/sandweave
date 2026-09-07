FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive container=docker
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d && apt-get update && apt-get install -y --no-install-recommends systemd systemd-sysv dbus sudo ca-certificates curl iptables iproute2 docker.io apache2 libapache2-mod-php php-cli php-mysql php-xml php-mbstring php-curl php-zip php-gd php-intl php-soap php-xmlrpc php-bcmath mariadb-client unzip && rm -rf /var/lib/apt/lists/*
COPY moodle-4.5.13.tgz /tmp/moodle.tgz
RUN tar -xzf /tmp/moodle.tgz -C /var/www/html && rm /tmp/moodle.tgz /var/www/html/index.html && mkdir -p /etc/moodle /var/moodledata && ln -s /etc/moodle/config.php /var/www/html/moodle/config.php && chown -R www-data:www-data /var/moodledata /etc/moodle && useradd -m -u 1000 -s /bin/bash ga && usermod -aG sudo,docker ga && printf 'ga ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/lab-ga
COPY moodle.conf /etc/apache2/sites-available/moodle.conf
COPY 99-lab.ini /etc/php/8.1/mods-available/lab.ini
RUN chmod 644 /etc/php/8.1/mods-available/lab.ini /etc/apache2/sites-available/moodle.conf && phpenmod lab && a2dissite 000-default && a2ensite moodle && a2enmod rewrite && systemctl enable apache2 docker && systemctl mask getty.target console-getty.service systemd-logind.service
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
