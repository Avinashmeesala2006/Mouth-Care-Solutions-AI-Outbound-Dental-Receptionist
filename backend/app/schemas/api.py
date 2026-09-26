from pydantic import BaseModel, Field
from typing import Any
class AgentRequest(BaseModel): message: str = Field(min_length=1, max_length=2000); session_id: str|None=None
class HoldRequest(BaseModel): slot_id: str; session_id: str; expiry_seconds: int=300
class ConfirmRequest(BaseModel): hold_token: str; patient_details: dict[str, Any]; consent: bool; idempotency_key: str
class RescheduleRequest(BaseModel): booking_reference: str; new_slot_id: str; verification: str
class CancelRequest(BaseModel): booking_reference: str; reason: str; verification: str
class CallbackRequest(BaseModel): patient_contact: str; topic: str; preferred_window: str
class KnowledgeRequest(BaseModel): query: str; locale: str='en-IN'
class AdminLoginRequest(BaseModel): email: str = Field(min_length=3, max_length=320); password: str = Field(min_length=8, max_length=256)
class CallRequest(BaseModel): name: str = Field(min_length=2, max_length=120); patient_contact: str = Field(min_length=5, max_length=40); preferred_window: str = Field(min_length=2, max_length=80); topic: str = Field(min_length=2, max_length=300); consent: bool
