"""
Pydantic schemas for SOS module requests, responses and web sockets.
"""
from datetime import datetime
from typing import List, Optional, Any
from pydantic import BaseModel, Field, ConfigDict
from app.models.sos import SOSType, MessageType, SOSStatus, DeliveryMethod

class SOSLocationSchema(BaseModel):
    lat: float = Field(..., description="Latitude coordinate", ge=-90, le=90)
    lng: float = Field(..., description="Longitude coordinate", ge=-180, le=180)
    altitude: Optional[float] = Field(None, description="Altitude in meters")
    accuracy: Optional[float] = Field(None, description="Location accuracy in meters")
    heading: Optional[float] = Field(None, description="Heading in degrees")
    speed: Optional[float] = Field(None, description="Speed in meters per second")
    provider: Optional[str] = Field(None, description="Location provider (e.g., GPS, Network)")

class RelayNodeSchema(BaseModel):
    device_id: str
    timestamp: datetime
    location: Optional[SOSLocationSchema] = None
    rssi: Optional[int] = None

class SOSCreateRequest(BaseModel):
    sos_id: str = Field(..., description="Client-generated unique ID to prevent duplicates")
    user_id: str = Field(..., description="ID of the user triggering SOS")
    user_name: str = Field(..., description="Name of the user")
    user_phone: Optional[str] = Field(None, description="Phone number of the user")
    sos_type: SOSType = Field(..., description="Category of SOS")
    message_type: MessageType = Field(MessageType.SOS_ALERT, description="Type of SOS message")
    location: SOSLocationSchema = Field(..., description="Location of the SOS event")
    delivery_method: DeliveryMethod = Field(DeliveryMethod.DIRECT_ONLINE, description="Method of alert delivery")
    hop_count: int = Field(0, description="Number of hops (if relayed)", ge=0)
    max_hops: int = Field(15, description="Maximum allowed hops")
    relay_chain: Optional[List[RelayNodeSchema]] = Field(None, description="Details of relay nodes")
    message: Optional[str] = Field(None, description="Optional text message")
    media_attachment_ids: Optional[List[str]] = Field(None, description="Attached media IDs")
    origin_device_id: Optional[str] = Field(None, description="Device ID of origin")
    ttl_seconds: int = Field(3600, description="Time-to-live for the SOS event")
    client_timestamp: datetime = Field(..., description="Timestamp from client device")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "sos_id": "uuid-1234-5678",
                "user_id": "user_01",
                "user_name": "John Doe",
                "sos_type": "MEDICAL",
                "location": {"lat": 12.9716, "lng": 77.5946, "accuracy": 5.0},
                "delivery_method": "DIRECT_ONLINE",
                "client_timestamp": "2023-10-01T12:00:00Z"
            }
        }
    )

class SOSCancelRequest(BaseModel):
    sos_id: str
    user_id: str
    reason: Optional[str] = None

class SOSResponse(BaseModel):
    success: bool
    sos_id: str
    message: str
    is_duplicate: bool = False
    server_timestamp: datetime = Field(default_factory=datetime.utcnow)

class SOSEventResponse(BaseModel):
    id: str
    sos_id: str
    user_id: str
    user_name: str
    user_phone: Optional[str]
    sos_type: SOSType
    message_type: MessageType
    status: SOSStatus
    delivery_method: DeliveryMethod
    location: SOSLocationSchema
    hop_count: int
    relay_chain: Optional[List[Any]]
    message: Optional[str]
    media_attachment_ids: Optional[List[str]]
    client_timestamp: datetime
    created_at: datetime
    distance_meters: Optional[float] = None
    acknowledged_by: Optional[List[str]] = None
    responders_en_route: int

    model_config = ConfigDict(from_attributes=True)

class ActiveSOSQuery(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    radius_km: float = Field(50, gt=0, le=1000)

class MediaUploadResponse(BaseModel):
    attachment_id: str
    file_url: str
    uploaded_at: datetime

class WebSocketSOSMessage(BaseModel):
    type: str = Field(..., description="'new_sos' | 'sos_update' | 'sos_cancel'")
    data: SOSEventResponse
