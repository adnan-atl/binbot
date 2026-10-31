from pydantic import BaseModel, Field


class AddressOut(BaseModel):
    uprn: str | None
    line: str
    house_number: str | None


class PostcodeOut(BaseModel):
    postcode: str
    council: str
    gss_code: str
    supported: bool
    needs_house_number: bool
    needs_uprn: bool
    addresses: list[AddressOut]


class StartIn(BaseModel):
    postcode: str
    uprn: str | None = None
    house_number: str | None = None
    address_line: str | None = None
    referral_code: str | None = None


class BinOut(BaseModel):
    setting_id: str | None = None
    bin_type_id: str | None = None
    label: str
    category: str
    colour: str | None = None
    days: list[str]
    frequency: str
    next: str | None
    enabled: bool = True
    manual: bool = False


class StartOut(BaseModel):
    user_id: str
    property_id: str
    council: str
    source: str  # council | manual | demo
    bins: list[BinOut]
    error: str | None = None


class BinIn(BaseModel):
    setting_id: str | None = None  # existing setting to update; omit to add
    label: str
    category: str
    enabled: bool = True
    days: list[str] | None = None  # when set: manual rule
    frequency: str | None = None
    anchor: str | None = None


class BinsIn(BaseModel):
    bins: list[BinIn]
    removed: list[str] = Field(default_factory=list)  # setting ids


class PhoneIn(BaseModel):
    phone: str
    consent: bool = True


class PrefsIn(BaseModel):
    mode: str  # night_before | morning_of
    time: str  # "19:00"


class ReferralOut(BaseModel):
    code: str
    link: str
    share_text: str
    referrals: int
    level: int
    level_name: str
    next_at: int | None


class ReminderOut(BaseModel):
    collection_date: str
    send_at: str
    bins: list[dict]
    status: str


class InboundIn(BaseModel):
    phone: str
    text: str
