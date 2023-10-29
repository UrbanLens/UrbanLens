export async function fetchSavedPlaces(id_token) {
  const response = await fetch('https://maps.googleapis.com/maps/api/place/nearbysearch/json?location=-33.8670522,151.1957362&radius=1500&key=YOUR_API_KEY', {
    headers: {
      'Authorization': `Bearer ${id_token}`
    }
  });

  if (response.ok) {
    const data = await response.json();
    return data.results;
  } else {
    console.error('Failed to fetch saved places');
    return [];
  }
}
