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
      fetch('/api/saved-places', {
        headers: {
          'Authorization': `Bearer ${id_token}`
        }
      })
      .then(response => response.json())
      .then(data => savedPlaces = data);
    });
  };
</script>

<div>
  <h1>Login</h1>
  <button on:click={login}>Login with Google</button>
</div>
