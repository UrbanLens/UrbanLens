<script>
  import { onMount } from 'svelte';
  import { googleLogin } from '../../lib/googleLogin.js';
  import { fetchSavedPlaces } from '../../lib/fetchSavedPlaces.js';

  let savedPlaces = [];

  const login = async () => {
    const id_token = await googleLogin();
    savedPlaces = await fetchSavedPlaces(id_token);
    sendSavedPlacesToBackend(savedPlaces, id_token);
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
