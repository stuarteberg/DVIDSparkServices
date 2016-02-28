from DVIDSparkServices.workflow.dvidworkflow import DVIDWorkflow
from DVIDSparkServices.sparkdvid.sparkdvid import retrieve_node_service 

class CreateTiles(DVIDWorkflow):
    # schema for ingesting grayscale
    Schema = """
{ "$schema": "http://json-schema.org/schema#",
  "title": "Tool to Create DVID blocks from image slices",
  "type": "object",
  "properties": {
    "dvid-info": {
      "type": "object",
      "properties": {
        "dvid-server": {
          "description": "location of DVID server",
          "type": "string",
          "property": "dvid-server"
        },
        "uuid": {
          "description": "version node to store segmentation",
          "type": "string"
        },
        "grayname": {
          "description": "name of grayscale datatype",
          "type": "string"
        },
        "tilename": {
          "description": "name of tile datatype",
          "type": "string"
        }
      },
      "required" : ["dvid-server", "uuid", "grayname", "tilename"]
    },
    "options": {
      "type": "object",
      "properties": {
        "axis": { 
          "description": "axis to generate tile",
          "type": "string",
          "enum": ["xy", "xz", "yz"],
          "default": "xy"
        },
        "format": {
          "description": "compression used for tiles",
          "type": "string",
          "enum": ["png", "jpg"],
          "default": "png"
        }
      }
    }
  }
}
    """
    
    # calls the default initializer
    def __init__(self, config_filename):
        super(CreateTiles, self).__init__(config_filename, self.Schema, "Create Tiles")

    # creates tiles for dataset loaded as grayscale blocks
    def execute(self):
        # block size default
        BLKSIZE = 32
        
        # tile size default
        TILESIZE = 512
        
        server = str(self.config_data["dvid-info"]["dvid-server"])
        uuid = str(self.config_data["dvid-info"]["uuid"])
        grayname = str(self.config_data["dvid-info"]["grayname"])
        tilename = str(self.config_data["dvid-info"]["tilename"])
        
        # determine grayscale blk extants
        if not server.startswith("http://"):
            server = "http://" + server

        import requests
        req = requests.get(server + "/api/node/" + uuid + "/" + grayname + "/info")
        graymeta = req.json()
        
        xmin, ymin, zmin = graymeta["Extended"]["MinIndex"] 
        xmax, ymax, zmax = graymeta["Extended"]["MaxIndex"] 

        imformat = str(self.config_data["options"]["format"])
        # create tiles type and meta
        requests.post(server + "/api/repo/" + uuid + "/instance", json={"typename": "imagetile", "dataname": tilename, "format": imformat})

        MinTileCoord = [xmin*BLKSIZE/TILESIZE, ymin*BLKSIZE/TILESIZE, zmin*BLKSIZE/TILESIZE]
        MaxTileCoord = [xmax*BLKSIZE/TILESIZE, ymax*BLKSIZE/TILESIZE, zmax*BLKSIZE/TILESIZE]
        
        # get max level by just finding max tile coord
        maxval = max(MaxTileCoord)
        minval = abs(min(MinTileCoord))
        maxval = max(minval, maxval) + 1
        import math
        maxlevel = int(math.log(maxval)/math.log(2))

        tilemeta = {}
        tilemeta["MinTileCoord"] = MinTileCoord
        tilemeta["MaxTileCoord"] = MaxTileCoord
        tilemeta["Levels"] = {}
        currres = 10.0 # just use as placeholder for now
        for level in range(0, maxlevel+1):
            tilemeta["Levels"][str(level)] = { "Resolution" : [currres, currres, currres], "TileSize": [TILESIZE, TILESIZE, TILESIZE]}
            currres *= 2
        
        requests.post(server + "/api/node/" + uuid + "/" + tilename + "/metadata", json=tilemeta)
        
        numiters = zmax+1
        axis = str(self.config_data["options"]["axis"])

        if axis == "xz":
            numiters = ymax+1 
        elif axis == "yz":
            numiters = xmax+1

        # retrieve 32 slices at a time and generate all tiles
        # TODO: only fetch 1 slice at a time if 32 slices cannot fit in memory
        blkiters = self.sparkdvid_context.sc.parallelize(range(0,numiters)) 
        
        def retrieveslices(blknum):
            # grab slice with 3d volume call
            node_service = retrieve_node_service(server, uuid)
            vol = None
           
            if axis == "xy": 
                vol = node_service.get_gray3D( str(grayname), (
                                                (xmax+1)*BLKSIZE-xmin*BLKSIZE,
                                                (ymax+1)*BLKSIZE-ymin*BLKSIZE,
                                                BLKSIZE),
                                                (xmin*BLKSIZE, ymin*BLKSIZE, blknum*BLKSIZE))
                vol = vol.transpose((2,1,0))
            elif axis == "xz":
                vol = node_service.get_gray3D( str(grayname), (
                                                (xmax+1)*BLKSIZE-xmin*BLKSIZE,
                                                BLKSIZE,
                                                (zmax+1)*BLKSIZE-zmin*BLKSIZE),
                                                (xmin*BLKSIZE, blknum*BLKSIZE, zmin*BLKSIZE))
                vol = vol.transpose((1,2,0))
            
            else:
                vol = node_service.get_gray3D( str(grayname), (
                                                BLKSIZE,
                                                (ymax+1)*BLKSIZE-ymin*BLKSIZE,
                                                (zmax+1)*BLKSIZE-zmin*BLKSIZE),
                                                (blknum*BLKSIZE, ymin*BLKSIZE, zmin*BLKSIZE))
                vol = vol.transpose((0,2,1))
                
            return (blknum, vol)

        imagedata = blkiters.map(retrieveslices)

        # ?! assume 0,0 starting coordinate for now for debuggin simplicity
        def writeimagepyramid(vol3d):
            blknum, vol = vol3d
            
            from PIL import Image
            from scipy import ndimage
            import StringIO
            import numpy

            # iterate slice by slice
            for slicenum in range(0, BLKSIZE):
                imslice = vol[slicenum, :, :]
                imlevels = []
                imlevels.append(imslice)
                # use generic downsample algorithm
                for level in range(1, maxlevel+1):
                    dim1, dim2 = imlevels[level-1].shape
                    # go to max level regardless of actual image size
                    #if dim1 < TILESIZE and dim2 < TILESIZE:
                        # image size is already smaller even though not at max level
                        #print "Not at max level"
                    #    break
                    imlevels.append(ndimage.interpolation.zoom(imlevels[level-1], 0.5)) 

                # write pyramid for each slice using custom request
                for levelnum in range(0, len(imlevels)):
                    levelslice = imlevels[levelnum]
                    dim1, dim2 = levelslice.shape

                    num1tiles = (dim1-1)/TILESIZE + 1
                    num2tiles = (dim2-1)/TILESIZE + 1

                    for iter1 in range(0, num1tiles):
                        for iter2 in range(0, num2tiles):
                            # extract tile
                            tileholder = numpy.zeros((TILESIZE, TILESIZE), numpy.uint8)
                            min1 = iter1*TILESIZE
                            min2 = iter2*TILESIZE
                            tileslice = levelslice[min1:min1+TILESIZE, min2:min2+TILESIZE]
                            t1, t2 = tileslice.shape
                            tileholder[0:t1, 0:t2] = tileslice

                            # write tileholder to dvid
                            buf = StringIO.StringIO() 
                            img = Image.frombuffer('L', (TILESIZE, TILESIZE), tileholder.tostring(), 'raw', 'L', 0, 1)
                            imformatpil = imformat
                            if imformat == "jpg":
                                imformatpil = "jpeg"
                            img.save(buf, format=imformatpil)

                            if axis == "xy":
                                requests.post(server + "/api/node/" + uuid + "/" + tilename + "/tile/" + axis + "/" + str(levelnum) + "/" + str(iter2) + "_" + str(iter1) + "_" + str(slicenum+blknum*BLKSIZE) , data=buf.getvalue())
                            elif axis == "xz":
                                requests.post(server + "/api/node/" + uuid + "/" + tilename + "/tile/" + axis + "/" + str(levelnum) + "/" + str(iter2) + "_" + str(slicenum+blknum*BLKSIZE) + "_" + str(iter1) , data=buf.getvalue())
                            else:
                                requests.post(server + "/api/node/" + uuid + "/" + tilename + "/tile/" + axis + "/" + str(levelnum) + "/" + str(slicenum+blknum*BLKSIZE) + "_" + str(iter2) + "_" + str(iter1) , data=buf.getvalue())

                            buf.close()

        imagedata.foreach(writeimagepyramid)



    @staticmethod
    def dumpschema():
        return CreateTiles.Schema



