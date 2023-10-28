import replace from 'rollup-plugin-replace';
import dotenv from 'dotenv';

// Load environment variables
dotenv.config();

export default {
	// ...existing config
	plugins: [
		// ...existing plugins
		replace({
			'__GOOGLE_MAPS_API_URL__': `https://maps.googleapis.com/maps/api/js?key=${process.env.GOOGLE_MAPS_API_KEY}`,
			preventAssignment: true
		}),
	],
};
