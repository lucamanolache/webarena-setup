#!/bin/bash

# stop if any error occur
set -e

source 00_vars.sh

# reddit - make server more responsive
podman exec forum sed -i \
  -e 's/^pm.max_children = .*/pm.max_children = 32/' \
  -e 's/^pm.start_servers = .*/pm.start_servers = 10/' \
  -e 's/^pm.min_spare_servers = .*/pm.min_spare_servers = 5/' \
  -e 's/^pm.max_spare_servers = .*/pm.max_spare_servers = 20/' \
  -e 's/^;pm.max_requests = .*/pm.max_requests = 500/' \
  /usr/local/etc/php-fpm.d/www.conf
podman exec forum supervisorctl restart php-fpm

# classifieds
podman exec classifieds_db mysql -u root -ppassword osclass -e 'source podman-entrypoint-initdb.d/osclass_craigslist.sql'  # Populate DB with content

# shopping
podman exec shopping /var/www/magento2/bin/magento setup:store-config:set --base-url="http://$PUBLIC_HOSTNAME:$SHOPPING_PORT" # no trailing /
podman exec shopping mysql -u magentouser -pMyPassword magentodb -e  "UPDATE core_config_data SET value='http://$PUBLIC_HOSTNAME:$SHOPPING_PORT/' WHERE path = 'web/secure/base_url';"
podman exec shopping /var/www/magento2/bin/magento cache:flush

# Disable re-indexing of products
podman exec shopping /var/www/magento2/bin/magento indexer:set-mode schedule catalogrule_product
podman exec shopping /var/www/magento2/bin/magento indexer:set-mode schedule catalogrule_rule
podman exec shopping /var/www/magento2/bin/magento indexer:set-mode schedule catalogsearch_fulltext
podman exec shopping /var/www/magento2/bin/magento indexer:set-mode schedule catalog_category_product
podman exec shopping /var/www/magento2/bin/magento indexer:set-mode schedule customer_grid
podman exec shopping /var/www/magento2/bin/magento indexer:set-mode schedule design_config_grid
podman exec shopping /var/www/magento2/bin/magento indexer:set-mode schedule inventory
podman exec shopping /var/www/magento2/bin/magento indexer:set-mode schedule catalog_product_category
podman exec shopping /var/www/magento2/bin/magento indexer:set-mode schedule catalog_product_attribute
podman exec shopping /var/www/magento2/bin/magento indexer:set-mode schedule catalog_product_price
podman exec shopping /var/www/magento2/bin/magento indexer:set-mode schedule cataloginventory_stock
