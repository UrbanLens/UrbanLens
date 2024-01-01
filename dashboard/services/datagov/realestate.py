"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    realestate.py                                                                                        *
*        Path:    /dashboard/services/datagov/realestate.py                                                            *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-01-01                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
import requests

class RealEstateSalesGateway:
    """
    Gateway for accessing real estate sales data from data.gov
    """
    
    def __init__(self):
        self.base_url = 'https://data.ct.gov/api/views/5mzw-sjtu/rows.json'
        self.data_dictionary_url = 'https://data.ct.gov/api/views/5mzw-sjtu/columns.json'
        self.session = requests.Session()

    def get_sales_data(self, access_type='DOWNLOAD'):
        """
        Fetch the real estate sales data.
        """
        params = {'accessType': access_type}
        response = self.session.get(self.base_url, params=params)
        response.raise_for_status()
        return response.json()

    def get_data_dictionary(self):
        """
        Fetch the data dictionary to understand the structure of the real estate data.
        """
        response = self.session.get(self.data_dictionary_url)
        response.raise_for_status()
        return response.json()