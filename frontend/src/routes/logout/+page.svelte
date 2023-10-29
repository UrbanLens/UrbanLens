<script>
  const logout = async () => {
    const response = await fetch('/api/logout', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      }
    });

    if (response.ok) {
      // Redirect to home page
      window.location.href = '/';
    } else {
      // Show error message
      console.error('Logout failed');
    }
  };

  // Call logout function when the component is mounted
  logout();
</script>

<div>
  <h1>Logging out...</h1>
</div>
