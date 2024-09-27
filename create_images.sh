IMAGE_NAME=mg-pythia-delphes
IMAGE_HUB_NAME=franaln/$IMAGE_NAME
IMAGES_DIR=/mnt/R5/images

tags=("3.3.2" "latest")


for tag in "${tags[@]}" ; do

    echo "building docker image for tag=$tag..."
    docker build -t $IMAGE_NAME:$tag -f dockerfiles/$IMAGE_NAME-$tag.Dockerfile .

    echo "tagging and pushing to docker hub tag=$tag"
    docker tag "$IMAGE_NAME:$tag" "$IMAGE_HUB_NAME:$tag"
    docker push $IMAGE_HUB_NAME:$tag

    echo "building apptainer image for tag=$tag..."
    apptainer build $IMAGE_NAME-$tag.sif docker://$IMAGE_HUB_NAME:$tag

    echo "copying sif image to jupiter directory"
    scp $IMAGE_NAME-$tag.sif $IMAGES_DIR/$IMAGE_NAME-$tag.sif

done
