import requests

from bin.utils.settings import GOOGLE_LENS_API_KEY, GOOGLE_LENS_URL, INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_GRAPH_URL


def fetch_urbex_posts(hashtag):
    """Fetch popular Instagram posts for a hashtag."""
    response = requests.get(f"{INSTAGRAM_GRAPH_URL}search?access_token={INSTAGRAM_ACCESS_TOKEN}&q={hashtag}", timeout=60)
    return response.json()


def identify_image_location(image_url):
    """Identify an image's location via image search."""
    response = requests.post(GOOGLE_LENS_URL, json={"image_url": image_url}, headers={"Authorization": f"Bearer {GOOGLE_LENS_API_KEY}"}, timeout=60)
    return response.json()


def main():
    hashtag = "urbex"
    posts = fetch_urbex_posts(hashtag)

    for post in posts:
        image_url = post["image_url"]
        location = identify_image_location(image_url)
        print(f"Image URL: {image_url}, Identified Location: {location}")


if __name__ == "__main__":
    main()
