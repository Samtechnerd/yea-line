idf_component_register(SRCS "main.c" "cfg.c" "audio.c" "bt.c" "sip.c" "net.c"
                       INCLUDE_DIRS "."
                       REQUIRES bt nvs_flash esp_wifi esp_netif esp_event esp_http_server esp_timer mbedtls lwip esp_system)
