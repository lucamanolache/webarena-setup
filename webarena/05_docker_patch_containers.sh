#!/bin/bash

# stop if any error occur
set -e

source 00_vars.sh

# Wait for MySQL to be ready in shopping container
echo "Waiting for MySQL in shopping container to be ready..."
MAX_RETRIES=30
RETRY_INTERVAL=5
for i in $(seq 1 $MAX_RETRIES); do
    if podman exec shopping mysql -u magentouser -pMyPassword magentodb -e "SELECT 1" > /dev/null 2>&1; then
        echo "MySQL is ready!"
        break
    fi
    if [ $i -eq $MAX_RETRIES ]; then
        echo "ERROR: MySQL in shopping container did not become ready after $((MAX_RETRIES * RETRY_INTERVAL)) seconds"
        exit 1
    fi
    echo "MySQL not ready yet, waiting... (attempt $i/$MAX_RETRIES)"
    sleep $RETRY_INTERVAL
done

# reddit - make server more responsive
podman exec forum sed -i \
  -e 's/^pm.max_children = .*/pm.max_children = 32/' \
  -e 's/^pm.start_servers = .*/pm.start_servers = 10/' \
  -e 's/^pm.min_spare_servers = .*/pm.min_spare_servers = 5/' \
  -e 's/^pm.max_spare_servers = .*/pm.max_spare_servers = 20/' \
  -e 's/^;pm.max_requests = .*/pm.max_requests = 500/' \
  /usr/local/etc/php-fpm.d/www.conf
podman exec forum supervisorctl restart php-fpm

# shopping + shopping admin
podman exec shopping /var/www/magento2/bin/magento setup:store-config:set --base-url="http://$PUBLIC_HOSTNAME:$SHOPPING_PORT" # no trailing /
podman exec shopping mysql -u magentouser -pMyPassword magentodb -e  "UPDATE core_config_data SET value='http://$PUBLIC_HOSTNAME:$SHOPPING_PORT/' WHERE path = 'web/secure/base_url';"
# remove the requirement to reset password
podman exec shopping_admin php /var/www/magento2/bin/magento config:set admin/security/password_is_forced 0
podman exec shopping_admin php /var/www/magento2/bin/magento config:set admin/security/password_lifetime 0
podman exec shopping /var/www/magento2/bin/magento cache:flush

podman exec shopping_admin /var/www/magento2/bin/magento setup:store-config:set --base-url="http://$PUBLIC_HOSTNAME:$SHOPPING_ADMIN_PORT"
podman exec shopping_admin mysql -u magentouser -pMyPassword magentodb -e  "UPDATE core_config_data SET value='http://$PUBLIC_HOSTNAME:$SHOPPING_ADMIN_PORT/' WHERE path = 'web/secure/base_url';"
podman exec shopping_admin /var/www/magento2/bin/magento cache:flush

# gitlab
podman exec gitlab sed -i "s|^external_url.*|external_url 'http://$PUBLIC_HOSTNAME:$GITLAB_PORT'|" /etc/gitlab/gitlab.rb
podman exec gitlab bash -c "grep -q 'puma\[\"worker_processes\"\]' /etc/gitlab/gitlab.rb || printf '\npuma[\"worker_processes\"] = 4' >> /etc/gitlab/gitlab.rb"  # bugfix https://github.com/ServiceNow/BrowserGym/issues/285
podman exec gitlab bash -c "grep -q 'prometheus_monitoring\[\"enable\"\]' /etc/gitlab/gitlab.rb || printf '\nprometheus_monitoring[\"enable\"] = false' >> /etc/gitlab/gitlab.rb"
podman exec gitlab gitlab-ctl reconfigure

# maps
podman exec openstreetmap-website-web-1 bin/rails db:migrate RAILS_ENV=development
