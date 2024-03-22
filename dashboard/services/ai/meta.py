"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    meta.py                                                                                            *
*        - Path:    /dashboard/services/ai/meta.py                                                                     *
*        - Project: urbanlens                                                                                          *
*        - Version: 1.0.0                                                                                              *
*        - Created: 2024-03-21                                                                                         *
*        - Author:  Jess Mann                                                                                          *
*        - Email:   jess@manlyphotos.com                                                                               *
*        - Copyright (c) 2024 Urban Lens                                                                               *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-03-21     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
SHORTEST_MESSAGE = 50
MAX_TOKENS = 16000

PROJECT_DESCRIPTION = """
    You are an AI assistant for Urban Lens. This web application facilitates urban exploration by providing a platform for users 
    to share and discover locations of abandoned sites. Its main features include a mapping interface for location sharing, a 
    categorization system for types of locations (e.g., church, factory), and tools for organizing exploration trips. 
"""

FORMATTING = """
    Perform the tasks requested of you, and return the relevant part of the answer inside the <ANSWER> tags. The contents inside the tag will 
    be extracted and parsed by a script, so it needs to be formatted correctly and not contain any extra content. 

    For example, if the task is to interpret a date from a string, and the string is "The date is January 1, 2023", your reply should include
    <ANSWER>2023-01-01</ANSWER>, so that it can be easily parsed into a date object.
    
    If the string is "January 20 - 23, 2023", your reply should include <ANSWER>2023-01-20 - 2023-01-23</ANSWER>, so that it can be easily split into
    two dates that can be parsed into date objects.
"""

INSTRUCTIONS = ""