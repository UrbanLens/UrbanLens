"""

	Metadata:

		File: consumers.py
		Project: UrbanLens

		Author: Jess Mann
		Email: jess@urbanlens.org

		-----


		Modified By: Jess Mann

		-----

		Copyright (c) 2023 Urban Lens
"""
import json
from channels.generic.websocket import AsyncWebsocketConsumer

class RequestStatusConsumer(AsyncWebsocketConsumer):
	async def connect(self):
		self.room_name = 'request_status'
		self.room_group_name = f'updates_{self.room_name}'

		await self.channel_layer.group_add(self.room_group_name, self.channel_name)
		await self.accept()

	async def disconnect(self, close_code):
		await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

	async def receive(self, text_data):
		pass

	async def send_status(self, event):
		message = event['message']
		await self.send(text_data=json.dumps({'message': message}))
