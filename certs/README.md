# TLS certificates

- `russian_trusted_root_ca.pem` — Russian Trusted Root CA (Mintsifry / НУЦ).  
  Required for HTTPS to `platform-api2.max.ru` (MAX Bot API).  
  Source: [Госуслуги — сертификаты](https://www.gosuslugi.ru/crt) (`russian_trusted_root_ca_pem.crt`).

`max_api.py` appends this root to the certifi bundle for Max API calls only.
