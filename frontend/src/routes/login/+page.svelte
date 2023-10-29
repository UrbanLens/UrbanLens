<script>
  import { onMount } from 'svelte';

  let auth2;

  onMount(() => {
    gapi.load('auth2', () => {
      auth2 = gapi.auth2.init({
        client_id: 'YOUR_CLIENT_ID',
      });
    });
  });

  let savedPlaces = [];

  const login = () => {
    auth2.signIn().then(googleUser => {
      const id_token = googleUser.getAuthResponse().id_token;
      // Send the ID token to your server

      // Fetch the list of saved places for the user
      getSavedPlaces(id_token);
    });
  };

  const getSavedPlaces = async (id_token) => {
    const response = await fetch('https://maps.googleapis.com/maps/api/place/nearbysearch/json?location=-33.8670522,151.1957362&radius=1500&key=YOUR_API_KEY', {
      headers: {
        'Authorization': `Bearer ${id_token}`
      }
    });

    if (response.ok) {
      const data = await response.json();
      savedPlaces = data.results;
      sendSavedPlacesToBackend(savedPlaces, id_token);
    } else {
      console.error('Failed to fetch saved places');
    }
  };

  const sendSavedPlacesToBackend = async (savedPlaces, id_token) => {
    const response = await fetch('/api/locations', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${id_token}`
      },
      body: JSON.stringify(savedPlaces)
    });

    if (!response.ok) {
      console.error('Failed to send saved places to backend');
    }
  };
</script>

<div>
  <h1>Login</h1>
  <button on:click={login}>Login with Google</button>
</div>
