import requests
from dashboard.services.gateway import Gateway

class NewsGateway(Gateway):
    """
    Gateway for a News API.
    """
    
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = 'https://newsapi.google.com/v2/everything'  # Google News API URL

    def get_news(self, location):
        """
        Fetch recent news articles about the given location from the News API.
        """
        params = {
            'q': location,
            'apiKey': self.api_key
        }

        response = requests.get(self.base_url, params=params)
        response.raise_for_status()
        
        news_data = response.json().get('articles', [])
        return news_data
