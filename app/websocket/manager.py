"""
WebSocket Connection Manager to handle connected clients and broadcasts.
"""
import asyncio
import json
from typing import Dict
from fastapi import WebSocket
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
        previous = self.active_connections.get(client_id)
        self.active_connections[client_id] = websocket
        if previous and previous is not websocket:
            await previous.close(code=1000)
        
        # Start heartbeat task if it's the first connection and task is not running
        if not self._heartbeat_task or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat())

    def disconnect(self, client_id: str, connection: WebSocket | None = None):
        """
        Remove a disconnected client from the manager.
        """
        if client_id in self.active_connections and (connection is None or self.active_connections[client_id] is connection):
            del self.active_connections[client_id]

    async def broadcast_sos(self, message: WebSocketSOSMessage):
        """
        Broadcast a new SOS message or update to all connected clients.
        """
        message_json = message.model_dump_json()
        disconnected_clients = []
        for client_id, connection in list(self.active_connections.items()):
            try:
                await asyncio.wait_for(connection.send_text(message_json), timeout=5)
            except Exception:
                # If sending fails, assume disconnected
                disconnected_clients.append((client_id, connection))
        
        for client_id, connection in disconnected_clients:
            self.disconnect(client_id, connection)

    async def send_to_client(self, client_id: str, message: WebSocketSOSMessage):
        """
        Send a message to a specific client.
        """
        if client_id in self.active_connections:
            connection = self.active_connections[client_id]
            try:
                await asyncio.wait_for(connection.send_text(message.model_dump_json()), timeout=5)
            except Exception:
                self.disconnect(client_id, connection)

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
                    await asyncio.wait_for(connection.send_text(heartbeat_msg), timeout=5)
                except Exception:
                    disconnected_clients.append((client_id, connection))
                    
            for client_id, connection in disconnected_clients:
                self.disconnect(client_id, connection)

    async def close(self):
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        connections = list(self.active_connections.values())
        self.active_connections.clear()
        await asyncio.gather(*(ws.close() for ws in connections), return_exceptions=True)

ws_manager = WebSocketManager()
