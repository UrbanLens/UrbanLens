import requests

from bin.utils.settings import GOOGLE_LENS_API_KEY, GOOGLE_LENS_URL, INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_GRAPH_URL


def fetch_urbex_posts(hashtag):
    """
    Fetches the most popular Instagram posts with the given hashtag.
    """
    # This is a placeholder for the actual API call, which would need to use the Instagram API.
    response = requests.get(f"{INSTAGRAM_GRAPH_URL}search?access_token={INSTAGRAM_ACCESS_TOKEN}&q={hashtag}")
    return response.json()


def identify_image_location(image_url):
    """
    Uses Google Lens or a similar service to identify the location of an image.
    """
    # This is a placeholder for the actual API call, which would need to use the Google Lens API or similar.
    response = requests.post(GOOGLE_LENS_URL, json={"image_url": image_url}, headers={"Authorization": f"Bearer {GOOGLE_LENS_API_KEY}"})
    return response.json()


def main():
    hashtag = "urbex"
    posts = fetch_urbex_posts(hashtag)

    for post in posts:
        image_url = post["image_url"]  # Placeholder for the actual image URL field
        location = identify_image_location(image_url)
        print(f"Image URL: {image_url}, Identified Location: {location}")


if __name__ == "__main__":
    main()
