from pydantic import BaseModel, StrictBool

from danmaku.domain.analysis import ModelConfigInput, ModelSettings
from danmaku.domain.sending import BilibiliCredential, BilibiliSettingsView, SendTiming
from danmaku.domain.workbench import Command


class SendingSettings(SendTiming):
    dual_account_enabled: StrictBool = False


class SendingSettingsUpdate(SendTiming):
    dual_account_enabled: StrictBool | None = None


class ReviewSettings(Command):
    uncheck_small_by_default: StrictBool = True


class SettingsUpdate(Command):
    bilibili: BilibiliCredential | None = None
    llm: ModelConfigInput | None = None
    review: ReviewSettings | None = None
    sending: SendingSettingsUpdate | None = None


class SettingsView(BaseModel):
    bilibili: BilibiliSettingsView
    llm: ModelSettings
    review: ReviewSettings
    sending: SendingSettings
