# Security and privacy

The voice dashboard is a local administrative interface, not a multi-user network service. Keep its default loopback binding and do not expose it through reverse proxies or port forwarding. It can manage processes, optional model containers and power actions.

LLM correction can send recognized text to a configured third-party provider. Leave it disabled for local-only processing. Recordings and logs may contain private speech; keep them outside version control.

Never attach API keys, recordings or unredacted logs to public issues. For a sensitive report, use GitHub private vulnerability reporting if enabled; otherwise contact the owner without posting sensitive material publicly.
