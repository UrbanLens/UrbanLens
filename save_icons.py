import os
import requests
from bs4 import BeautifulSoup

# URL of the material icons page
url = "https://fonts.google.com/icons"

# Directory to save the icons
dir_path = "/frontend/icons"

# Make the directory if it doesn't exist
os.makedirs(dir_path, exist_ok=True)

# Get the HTML of the page
response = requests.get(url)
soup = BeautifulSoup(response.text, 'html.parser')

# Find all the icon elements
icons = soup.find_all("i", class_="material-icons")

# For each icon
for icon in icons:
    # Get the name of the icon
    icon_name = icon.text

    # URL of the icon image
    icon_url = f"https://fonts.gstatic.com/s/i/materialicons/{icon_name}/v6/24px.svg?download=true"

    # Get the icon image
    response = requests.get(icon_url)

    # Save the icon image
    with open(f"{dir_path}/{icon_name}.svg", 'wb') as f:
        f.write(response.content)

    print(f"Saved {icon_name}.svg")
