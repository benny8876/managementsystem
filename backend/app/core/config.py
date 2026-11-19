from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Shwe Muse HMS"
    secret_key: str = "dev-secret-change-me-shwe-muse"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 480
    refresh_token_expire_days: int = 7
    database_url: str = "sqlite:///./shwemuse.db"
    seed_on_start: bool = True
    cors_origins: str = "http://localhost:5173,http://localhost:8080,http://127.0.0.1:5173"
    session_timeout_minutes: int = 30
    password_min_length: int = 8
    sms_enabled: bool = True
    whatsapp_enabled: bool = False
    demo_otp: str = "123456"
    # Shared secret a branch's scripts/push_sync.py cron job sends in the
    # X-Sync-Key header when pushing its daily rollup to the cloud Admin Panel
    # instance — there's no logged-in user for an unattended cron push, so this
    # is a separate, simpler auth path from the JWT login flow everything else uses.
    sync_shared_key: str = "dev-sync-key-change-me-shwe-muse"
    # Clinic offline server -> cloud: the clinic uploads a copy of its database
    # (POST /sync/mirror-upload) into mirror_dir. A second, read-only instance of
    # this app (mirror_mode=true, its own SECRET_KEY) serves that copy so the
    # owner can look at clinic data from the cloud.
    mirror_mode: bool = False
    mirror_dir: str = "/var/lib/shwemuse/clinic-mirror"
    # The hospital owner opens the clinic copy from the Owner Panel without a
    # second login: the main instance asks the mirror (over localhost, with this
    # shared key) for a session for the clinic's owner account.
    mirror_url: str = "http://127.0.0.1:8003"
    mirror_internal_key: str = ""
    # Site servers (the hospital's and the clinic's own LAN installs): which site
    # this is, and the cloud address the patients' QR links point to. On a site
    # the item list (and prices) come from the cloud — see site_sync.py.
    site: str = ""
    qr_base_url: str = ""
    # The cloud once the hospital runs its own server: item master, Central
    # Warehouse and Owner Panel only — counter staff use their site server.
    hub_mode: bool = False
    # Site servers whose uploads the cloud refuses for now (comma list, e.g.
    # "hospital" while a test install is still out there before go-live).
    sync_blocked_sites: str = ""

    @property
    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
