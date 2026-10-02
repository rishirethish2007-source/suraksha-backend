"""
WebSocket Connection Manager to handle connected clients and broadcasts.
"""
import asyncio
import json
from typing import Dict
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from app.schemas.sos import WebSocketSOSMessage
from app.config import settings

class WebSocketManager:
    """
    Manages active websocket connections and broadcasts messages.
    """
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self._heartbeat_task = None

    async def connect(self, websocket: WebSocket, client_id: str):
        """
        Accept a new websocket connection and store it.
        """
        await websocket.accept()
        self.active_connections[client_id] = websocket
        
        # Start heartbeat task if it's the first connection and task is not running
        if not self._heartbeat_task or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat())

    def disconnect(self, client_id: str):
        """
        Remove a disconnected client from the manager.
        """
        if client_id in self.active_connections:
            del self.active_connections[client_id]

    async def broadcast_sos(self, message: WebSocketSOSMessage):
        """
        Broadcast a new SOS message or update to all connected clients.
        """
        message_json = message.model_dump_json()
        disconnected_clients = []
        for client_id, connection in self.active_connections.items():
            try:
                await connection.send_text(message_json)
            except Exception:
                # If sending fails, assume disconnected
                disconnected_clients.append(client_id)
        
        for client_id in disconnected_clients:
            self.disconnect(client_id)

    async def send_to_client(self, client_id: str, message: WebSocketSOSMessage):
        """
        Send a message to a specific client.
        """
        if client_id in self.active_connections:
            connection = self.active_connections[client_id]
            try:
                await connection.send_text(message.model_dump_json())
            except Exception:
                self.disconnect(client_id)

    async def _heartbeat(self):
        """
        Periodic task to ping all connected clients to keep connections alive.
        """
        while True:
            await asyncio.sleep(settings.WS_HEARTBEAT_INTERVAL)
            if not self.active_connections:
                continue
                
            heartbeat_msg = json.dumps({"type": "ping", "timestamp": asyncio.get_event_loop().time()})
            disconnected_clients = []
            
            for client_id, connection in list(self.active_connections.items()):
                try:
                    await connection.send_text(heartbeat_msg)
                except Exception:
                    disconnected_clients.append(client_id)
                    
            for client_id in disconnected_clients:
                self.disconnect(client_id)

ws_manager = WebSocketManager()
